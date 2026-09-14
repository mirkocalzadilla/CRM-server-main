"""Redis Pub/Sub para notificaciones realtime al inbox `/crm` (§9, M5, slice 2b).

Contrato (lo consume el front en el repo web): un canal por organización,
`crm:events:{tenant_id}`, payload JSON. M5 publica `{"type": "handoff", ...}`;
M-CRM-api extiende el mismo canal con más tipos (card movida, toggle IA) y el
SSE (slice 2b) lo consume vía `subscribe`. Tipos vigentes: `card_moved`, `handoff`,
`ai_active_changed`, `card_attended` (`{card_id, conversation_id}` — 0036),
`agent_error` (`{conversation_id, category}` — el turno del agente falló fuera del
flujo de handoff, server#288; el front invalida board + card) y `receipt_needs_review`
(`{card_id, conversation_id}` — la validación automática dejó el comprobante en
revisión; el front invalida board + card + panel del comprobante). Espejo del patrón
de `dispatcher.py` (cliente fino + dispose).
"""

from __future__ import annotations

import asyncio
import json
import uuid
from collections.abc import AsyncGenerator

from redis.asyncio import Redis
from redis.asyncio.client import PubSub

from server.config import get_settings

CRM_CHANNEL_PREFIX = "crm:events:"

settings = get_settings()

redis_client: Redis = Redis.from_url(settings.redis_url, decode_responses=True)


def crm_channel(tenant_id: uuid.UUID) -> str:
    """Canal por tenant — el front se suscribe solo al suyo (sin fuga cross-tenant)."""
    return f"{CRM_CHANNEL_PREFIX}{tenant_id}"


def card_moved_event(
    *, card_id: uuid.UUID, stage: str, conversation_id: uuid.UUID, pipeline_kind: str
) -> dict[str, object]:
    """Payload unificado de `card_moved` — mismo shape desde `CardService.sync`
    y `BoardService.move_card`, así el front no ramifica por origen."""
    return {
        "type": "card_moved",
        "card_id": str(card_id),
        "stage": stage,
        "conversation_id": str(conversation_id),
        "pipeline_kind": pipeline_kind,
    }


class Publisher:
    """Wrapper fino de `PUBLISH` sobre `redis.asyncio`."""

    def __init__(self, client: Redis) -> None:
        self._client = client

    async def publish(self, channel: str, payload: dict[str, object]) -> None:
        await self._client.publish(channel, json.dumps(payload))


publisher = Publisher(redis_client)


# redis-py 8 fija socket_timeout=5s por defecto: una lectura bloqueante (`listen()`)
# revienta sin tráfico. Poll con timeout explícito → devuelve None y se reintenta.
_POLL_TIMEOUT_SECONDS = 1.0


async def subscribe(channel: str) -> AsyncGenerator[str, None]:
    """Suscripción a un canal: itera los payloads crudos publicados (uno por mensaje).

    Encapsula el ciclo completo del pubsub de Redis (subscribe → poll → cleanup);
    una conexión pubsub del pool por consumidor. El cleanup va con `shield` para que
    corra completo aunque el consumidor sea cancelado (desconexión de un cliente SSE).
    """
    pubsub = redis_client.pubsub()
    await pubsub.subscribe(channel)
    try:
        while True:
            message = await pubsub.get_message(
                ignore_subscribe_messages=True, timeout=_POLL_TIMEOUT_SECONDS
            )
            if message is None:
                continue
            data = message.get("data")
            if message.get("type") == "message" and isinstance(data, str):
                yield data
    finally:
        await asyncio.shield(_release_pubsub(pubsub, channel))


async def _release_pubsub(pubsub: PubSub, channel: str) -> None:
    await pubsub.unsubscribe(channel)
    await pubsub.aclose()  # type: ignore[no-untyped-call]  # redis-py no tipa PubSub.aclose


async def dispose_publisher() -> None:
    await redis_client.aclose()
