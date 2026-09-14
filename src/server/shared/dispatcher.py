"""Redis-backed dispatch queue — shared contract for producer (webhook, M1/C)
and consumer (worker, M1/N).

List name, lock key format and payload shape are the contract: changing them
here means updating docs/SPECS_MVP.md "M1 en detalle" too.
"""

import json
import uuid

from redis.asyncio import Redis
from redis.exceptions import TimeoutError as RedisTimeoutError

from server.config import get_settings

DISPATCH_LIST = "agent:dispatch"
# Cola aparte para la validación de comprobantes por visión. Separada y no un `kind`
# dentro de la misma lista porque el consumidor es único y secuencial: una llamada de
# visión de varios segundos en la cola de turnos congelaría las respuestas de TODOS los
# leads, de todos los tenants, mientras dura. Con dos colas, cada tipo de trabajo avanza
# a su ritmo y el worker atiende las dos en paralelo.
VISION_LIST = "agent:vision"
LOCK_KEY_PREFIX = "agent:lock:"
# Lock del job de visión: por mensaje (wamid), no por conversación. Dos jobs del mismo
# comprobante no corren a la vez, pero un comprobante no bloquea el turno del agente ni
# al revés. TTL más largo porque incluye una llamada a un modelo.
VISION_LOCK_PREFIX = "agent:vision:lock:"
VISION_LOCK_TTL = 180
DEFAULT_LOCK_TTL = 30

# Tipo de trabajo. Viaja en el payload además de estar implícito en la cola: hace el
# contrato explícito en los logs y permite validar que un item no se encoló mal.
JOB_AGENT_TURN = "agent_turn"
JOB_RECEIPT_VISION = "receipt_vision"
# Heartbeat del worker (#246): el loop la refresca en cada iteración; /health/deep chequea
# que exista. TTL holgado (>> los 2s del loop) para que un turno largo del agente no la
# deje expirar y dispare un falso "worker caído".
WORKER_HEARTBEAT_KEY = "worker:heartbeat"
WORKER_HEARTBEAT_TTL = 60

settings = get_settings()

redis_client: Redis = Redis.from_url(settings.redis_url, decode_responses=True)


class Dispatcher:
    """Thin async wrapper over Redis exposing the `agent:dispatch` contract."""

    def __init__(self, client: Redis) -> None:
        self._client = client

    async def enqueue(self, conversation_id: uuid.UUID, tenant_id: uuid.UUID) -> None:
        payload = json.dumps(
            {
                "conversation_id": str(conversation_id),
                "tenant_id": str(tenant_id),
                "kind": JOB_AGENT_TURN,
            }
        )
        await self._client.lpush(DISPATCH_LIST, payload)

    async def enqueue_vision(
        self, conversation_id: uuid.UUID, tenant_id: uuid.UUID, wamid: str
    ) -> None:
        """Encola la validación de un comprobante. `wamid` identifica el mensaje del
        lead, y es lo que hace el job idempotente: el mismo comprobante encolado dos
        veces se procesa una sola."""
        payload = json.dumps(
            {
                "conversation_id": str(conversation_id),
                "tenant_id": str(tenant_id),
                "wamid": wamid,
                "kind": JOB_RECEIPT_VISION,
            }
        )
        await self._client.lpush(VISION_LIST, payload)

    async def dequeue_vision(self, timeout: int = 0) -> dict[str, str] | None:
        return await self._pop(VISION_LIST, timeout)

    async def dequeue(self, timeout: int = 0) -> dict[str, str] | None:
        return await self._pop(DISPATCH_LIST, timeout)

    async def _pop(self, queue: str, timeout: int) -> dict[str, str] | None:
        try:
            result = await self._client.brpop([queue], timeout=timeout)
        except RedisTimeoutError:
            # brpop bloqueante agotó `timeout` con la lista vacía: redis-py corre el
            # deadline de lectura del socket contra el mismo timeout. Es "nada en la
            # ventana" → None, según el contrato; deja al consumer chequear shutdown.
            return None
        if result is None:
            return None
        _, raw = result
        decoded: dict[str, str] = json.loads(raw)
        return decoded

    async def acquire_lock(self, conversation_id: uuid.UUID, ttl: int = DEFAULT_LOCK_TTL) -> bool:
        acquired = await self._client.set(
            f"{LOCK_KEY_PREFIX}{conversation_id}", "1", nx=True, ex=ttl
        )
        return bool(acquired)

    async def release_lock(self, conversation_id: uuid.UUID) -> None:
        await self._client.delete(f"{LOCK_KEY_PREFIX}{conversation_id}")

    async def acquire_vision_lock(self, wamid: str, ttl: int = VISION_LOCK_TTL) -> bool:
        """Lock por comprobante. TTL largo: incluye una llamada a un modelo de visión."""
        acquired = await self._client.set(f"{VISION_LOCK_PREFIX}{wamid}", "1", nx=True, ex=ttl)
        return bool(acquired)

    async def release_vision_lock(self, wamid: str) -> None:
        await self._client.delete(f"{VISION_LOCK_PREFIX}{wamid}")

    async def beat(self) -> None:
        await self._client.set(WORKER_HEARTBEAT_KEY, "1", ex=WORKER_HEARTBEAT_TTL)


dispatcher = Dispatcher(redis_client)


async def dispose_dispatcher() -> None:
    await redis_client.aclose()
