"""Worker de dispatch (M1/N) — consume `agent:dispatch`, serializa por conversación
con lock y delega en un handler inyectado.

Proceso aparte de la API (`python -m server.worker`). En M1 el handler es un stub que
loguea; en la integración se inyecta `AgentService.process_message`. El `tenant_id`
viaja solo para filtrar queries aguas abajo — nunca entra al contexto del LLM.
Contrato de la cola/lock: `server.shared.dispatcher` (fuente única).
"""

from __future__ import annotations

import asyncio
import signal
import uuid
from collections.abc import Awaitable, Callable

import sentry_sdk
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from server.config import get_settings
from server.modules.agent.services.catch_up_service import run_catch_up
from server.modules.agent.services.dispatch_handler import agent_dispatch_handler
from server.modules.agent.services.vision_dispatch_handler import receipt_vision_handler
from server.shared.database import async_session_maker, dispose_engine
from server.shared.dispatcher import Dispatcher, dispatcher, dispose_dispatcher
from server.shared.logger import configure_logging, get_logger
from server.shared.observability import init_sentry
from server.shared.pubsub import dispose_publisher

logger = get_logger(__name__)

# Handler signature: AsyncSession por item (la abre el worker); integración inyecta
# AgentService.process_message. tenant_id presente para scoping, no para el LLM.
Handler = Callable[[uuid.UUID, uuid.UUID, AsyncSession], Awaitable[None]]
# Handler de visión: además del scoping recibe el wamid del comprobante a validar.
VisionHandler = Callable[[uuid.UUID, uuid.UUID, str, AsyncSession], Awaitable[None]]

DEQUEUE_TIMEOUT = 2  # cota de cuán seguido el loop chequea shutdown con la cola vacía
REQUEUE_BACKOFF = 0.2  # evita el hot-loop al re-encolar un item cuya conversación está lockeada


async def _stub_handler(
    conversation_id: uuid.UUID,
    tenant_id: uuid.UUID,
    session: AsyncSession,
) -> None:
    """M1 placeholder — loguea la recepción. La integración lo reemplaza por el agente."""
    logger.info("dispatch.received", conversation_id=str(conversation_id))


async def process_one(
    payload: dict[str, str],
    dispatcher: Dispatcher,
    session_maker: async_sessionmaker[AsyncSession],
    handler: Handler,
) -> None:
    """Procesa un item: toma lock → handler (session por item) → libera en finally.

    Si el lock está tomado (otro turno de la misma conversación en vuelo), re-encola
    el payload y sigue, sin procesar.
    """
    conversation_id = uuid.UUID(payload["conversation_id"])
    tenant_id = uuid.UUID(payload["tenant_id"])

    if not await dispatcher.acquire_lock(conversation_id):
        await dispatcher.enqueue(conversation_id=conversation_id, tenant_id=tenant_id)
        logger.info("dispatch.requeued_locked", conversation_id=str(conversation_id))
        # backoff: el item lockeado rebotaría a máxima velocidad hasta liberarse el lock
        await asyncio.sleep(REQUEUE_BACKOFF)
        return

    try:
        async with session_maker() as session:
            await handler(conversation_id, tenant_id, session)
    finally:
        await dispatcher.release_lock(conversation_id)


async def process_vision_one(
    payload: dict[str, str],
    dispatcher: Dispatcher,
    session_maker: async_sessionmaker[AsyncSession],
    handler: VisionHandler,
) -> None:
    """Procesa un comprobante: lock por wamid → handler → libera.

    El lock es por mensaje, no por conversación: dos jobs del mismo comprobante no
    corren a la vez, pero un comprobante nunca bloquea el turno del agente. Si el lock
    está tomado, el item se **descarta** en vez de re-encolarse — el job es idempotente
    por wamid, así que el que ya está corriendo hace el trabajo.
    """
    conversation_id = uuid.UUID(payload["conversation_id"])
    tenant_id = uuid.UUID(payload["tenant_id"])
    wamid = payload["wamid"]

    if not await dispatcher.acquire_vision_lock(wamid):
        logger.info("vision.skipped_locked", wamid=wamid)
        return
    try:
        async with session_maker() as session:
            await handler(conversation_id, tenant_id, wamid, session)
    finally:
        await dispatcher.release_vision_lock(wamid)


async def consume(
    stop: asyncio.Event,
    dispatcher: Dispatcher = dispatcher,
    session_maker: async_sessionmaker[AsyncSession] = async_session_maker,
    handler: Handler = _stub_handler,
) -> None:
    """Loop principal: dequeue (con timeout) → process_one, hasta que `stop` se setea."""
    logger.info("worker.started")
    while not stop.is_set():
        # Heartbeat para /health/deep (#246). Si Redis está caído igual moriríamos en el
        # dequeue de la línea siguiente, así que no agrega un modo de falla nuevo.
        await dispatcher.beat()
        payload = await dispatcher.dequeue(timeout=DEQUEUE_TIMEOUT)
        if payload is None:
            continue
        await process_one(payload, dispatcher, session_maker, handler)
    logger.info("worker.stopping")


async def consume_vision(
    stop: asyncio.Event,
    dispatcher: Dispatcher = dispatcher,
    session_maker: async_sessionmaker[AsyncSession] = async_session_maker,
    handler: VisionHandler | None = None,
) -> None:
    """Loop de validación de comprobantes, en paralelo al de turnos.

    Corre aparte porque leer un comprobante tarda segundos: en la misma cola que los
    turnos, cada comprobante retrasaría las respuestas de todos los leads del sistema.
    No late el heartbeat — de eso se encarga el loop de turnos, que nunca se bloquea
    acá, así que un comprobante lento no puede simular un worker caído.
    """
    if handler is None:
        return
    logger.info("worker.vision_started")
    while not stop.is_set():
        payload = await dispatcher.dequeue_vision(timeout=DEQUEUE_TIMEOUT)
        if payload is None:
            continue
        await process_vision_one(payload, dispatcher, session_maker, handler)
    logger.info("worker.vision_stopping")


def _install_signal_handlers(stop: asyncio.Event) -> None:
    """SIGINT/SIGTERM setean `stop`; el dequeue (con timeout) corta el loop sin colgar.

    `loop.add_signal_handler` en Unix (donde corre el worker): se integra con el
    wakeup-fd del loop y atiende la señal de forma confiable. Fallback a
    `signal.signal` en Windows, donde `add_signal_handler` no está soportado.
    """
    loop = asyncio.get_running_loop()

    def _request_stop() -> None:
        logger.info("worker.signal_received")
        stop.set()

    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(sig, _request_stop)
        except NotImplementedError:
            signal.signal(sig, lambda *_: stop.set())


async def _catch_up_on_start() -> None:
    """Al arrancar, re-encola los inbounds que quedaron sin atender tras una caída (#187).

    Aislado del loop: un fallo del barrido (DB/Redis) se loguea y no impide consumir —
    el objetivo es recuperar, no bloquear el arranque.
    """
    try:
        async with async_session_maker() as session:
            await run_catch_up(session, dispatcher)
    except Exception as exc:  # el catch-up es best-effort: nunca frena el arranque
        sentry_sdk.capture_exception(exc)
        logger.error("worker.catch_up_failed", error=str(exc))


async def main() -> None:
    configure_logging()
    init_sentry(get_settings())
    stop = asyncio.Event()
    _install_signal_handlers(stop)
    try:
        await _catch_up_on_start()
        # Los dos loops corren en paralelo: los turnos del agente no esperan a que
        # termine de leerse un comprobante, ni al revés.
        await asyncio.gather(
            consume(stop, handler=agent_dispatch_handler),
            consume_vision(stop, handler=receipt_vision_handler),
        )
    finally:
        await dispose_dispatcher()
        await dispose_publisher()
        await dispose_engine()
        logger.info("worker.stopped")


if __name__ == "__main__":
    asyncio.run(main())
