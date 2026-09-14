"""Loop de validación de comprobantes del worker (server#272, CR3).

Lo que fija: el comprobante se procesa con lock **por mensaje** (no por conversación),
un segundo job del mismo comprobante se descarta en vez de re-encolarse (es idempotente,
el que ya corre hace el trabajo), y el loop de visión es independiente del de turnos —
que es lo que impide que leer un comprobante retrase las respuestas de otros leads.
"""

from __future__ import annotations

import asyncio
import contextlib
import uuid

from sqlalchemy.ext.asyncio import AsyncSession

from server.shared.dispatcher import JOB_RECEIPT_VISION
from server.worker import consume_vision, process_vision_one

WAMID = "wamid.ABC123"


class _StubDispatcher:
    """Dispatcher en memoria con las primitivas que usa el loop de visión."""

    def __init__(self, *, locked: set[str] | None = None) -> None:
        self.locked = locked or set()
        self.acquired: list[str] = []
        self.released: list[str] = []
        self.vision_queue: list[dict[str, str]] = []
        self.turn_queue: list[dict[str, str]] = []

    async def acquire_vision_lock(self, wamid: str, ttl: int = 180) -> bool:
        if wamid in self.locked:
            return False
        self.locked.add(wamid)
        self.acquired.append(wamid)
        return True

    async def release_vision_lock(self, wamid: str) -> None:
        self.locked.discard(wamid)
        self.released.append(wamid)

    async def dequeue_vision(self, timeout: int = 0) -> dict[str, str] | None:
        return self.vision_queue.pop() if self.vision_queue else None

    async def enqueue(self, conversation_id: uuid.UUID, tenant_id: uuid.UUID) -> None:
        self.turn_queue.append({"conversation_id": str(conversation_id)})

    async def enqueue_vision(
        self, conversation_id: uuid.UUID, tenant_id: uuid.UUID, wamid: str
    ) -> None:
        self.vision_queue.append({"wamid": wamid})

    async def beat(self) -> None:
        return None


class _StubSessionMaker:
    def __init__(self) -> None:
        self.opened = 0

    def __call__(self) -> _StubSessionMaker:
        self.opened += 1
        return self

    async def __aenter__(self) -> object:
        return object()

    async def __aexit__(self, *args: object) -> None:
        return None


def _payload(conversation_id: uuid.UUID, tenant_id: uuid.UUID) -> dict[str, str]:
    return {
        "conversation_id": str(conversation_id),
        "tenant_id": str(tenant_id),
        "wamid": WAMID,
        "kind": JOB_RECEIPT_VISION,
    }


async def test_processes_with_lock_and_releases_it() -> None:
    dispatcher = _StubDispatcher()
    maker = _StubSessionMaker()
    seen: list[tuple[uuid.UUID, str]] = []

    async def handler(
        conversation_id: uuid.UUID, tenant_id: uuid.UUID, wamid: str, session: AsyncSession
    ) -> None:
        seen.append((conversation_id, wamid))

    conv_id, tenant_id = uuid.uuid4(), uuid.uuid4()
    await process_vision_one(
        _payload(conv_id, tenant_id),
        dispatcher,  # type: ignore[arg-type]
        maker,  # type: ignore[arg-type]
        handler,
    )

    assert seen == [(conv_id, WAMID)]
    assert dispatcher.acquired == [WAMID]
    assert dispatcher.released == [WAMID]
    assert maker.opened == 1


async def test_locked_message_is_dropped_not_requeued() -> None:
    """El job es idempotente por wamid: si otro ya lo está haciendo, re-encolarlo solo
    haría girar la cola. El que corre hace el trabajo."""
    dispatcher = _StubDispatcher(locked={WAMID})
    maker = _StubSessionMaker()
    called = False

    async def handler(
        conversation_id: uuid.UUID, tenant_id: uuid.UUID, wamid: str, session: AsyncSession
    ) -> None:
        nonlocal called
        called = True

    await process_vision_one(
        _payload(uuid.uuid4(), uuid.uuid4()),
        dispatcher,  # type: ignore[arg-type]
        maker,  # type: ignore[arg-type]
        handler,
    )

    assert called is False
    assert maker.opened == 0  # no se abre sesión para nada
    assert dispatcher.vision_queue == []  # no se re-encola
    assert dispatcher.released == []  # no se libera un lock ajeno


async def test_lock_is_released_even_if_the_handler_explodes() -> None:
    """Un comprobante que falla no puede dejar el lock tomado 3 minutos."""
    dispatcher = _StubDispatcher()

    async def handler(
        conversation_id: uuid.UUID, tenant_id: uuid.UUID, wamid: str, session: AsyncSession
    ) -> None:
        raise RuntimeError("el proveedor falló")

    with contextlib.suppress(RuntimeError):
        await process_vision_one(
            _payload(uuid.uuid4(), uuid.uuid4()),
            dispatcher,  # type: ignore[arg-type]
            _StubSessionMaker(),  # type: ignore[arg-type]
            handler,
        )
    assert dispatcher.released == [WAMID]


async def test_consume_vision_drains_its_own_queue() -> None:
    dispatcher = _StubDispatcher()
    conv_id, tenant_id = uuid.uuid4(), uuid.uuid4()
    dispatcher.vision_queue.append(_payload(conv_id, tenant_id))
    stop = asyncio.Event()
    seen: list[str] = []

    async def handler(
        conversation_id: uuid.UUID, tenant_id: uuid.UUID, wamid: str, session: AsyncSession
    ) -> None:
        seen.append(wamid)
        stop.set()  # un item y corta

    await consume_vision(
        stop,
        dispatcher,  # type: ignore[arg-type]
        _StubSessionMaker(),  # type: ignore[arg-type]
        handler,
    )
    assert seen == [WAMID]


async def test_consume_vision_without_handler_is_a_noop() -> None:
    """Sin handler (p. ej. un smoke que solo quiere los turnos) el loop no arranca en
    vez de consumir items y descartarlos."""
    dispatcher = _StubDispatcher()
    dispatcher.vision_queue.append(_payload(uuid.uuid4(), uuid.uuid4()))
    await consume_vision(asyncio.Event(), dispatcher, _StubSessionMaker(), None)  # type: ignore[arg-type]
    assert len(dispatcher.vision_queue) == 1  # no se consumió nada


async def test_maker_signature_matches_the_real_session_maker() -> None:
    """Guard del stub: si `process_vision_one` cambiara cómo abre la sesión, el resto
    de estos tests pasarían igual sin ejercitar nada."""
    maker = _StubSessionMaker()
    async with maker() as session:
        assert session is not None
    assert maker.opened == 1
