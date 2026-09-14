"""Tests del worker (loop puro, sin Redis ni DB reales). Ver SPECS_MVP §"M1 en detalle"."""

from __future__ import annotations

import asyncio
import uuid

from server.worker import consume, process_one


class FakeSession:
    async def __aenter__(self) -> FakeSession:
        return self

    async def __aexit__(self, *_: object) -> bool:
        return False


class FakeSessionMaker:
    def __init__(self) -> None:
        self.opened = 0

    def __call__(self) -> FakeSession:
        self.opened += 1
        return FakeSession()


class FakeDispatcher:
    """Stand-in del Dispatcher: cola en memoria + estado de lock controlable."""

    def __init__(self, items: list[dict[str, str]] | None = None, lock_free: bool = True) -> None:
        self.items = list(items or [])
        self.lock_free = lock_free
        self.enqueued: list[uuid.UUID] = []
        self.acquired: list[uuid.UUID] = []
        self.released: list[uuid.UUID] = []
        self.beats = 0

    async def beat(self) -> None:
        self.beats += 1

    async def dequeue(self, timeout: int = 0) -> dict[str, str] | None:
        return self.items.pop(0) if self.items else None

    async def enqueue(self, conversation_id: uuid.UUID, tenant_id: uuid.UUID) -> None:
        self.enqueued.append(conversation_id)

    async def acquire_lock(self, conversation_id: uuid.UUID, ttl: int = 30) -> bool:
        self.acquired.append(conversation_id)
        return self.lock_free

    async def release_lock(self, conversation_id: uuid.UUID) -> None:
        self.released.append(conversation_id)


def _payload() -> tuple[dict[str, str], uuid.UUID]:
    conv_id = uuid.uuid4()
    return {"conversation_id": str(conv_id), "tenant_id": str(uuid.uuid4())}, conv_id


def _recorder() -> tuple[list[uuid.UUID], object]:
    calls: list[uuid.UUID] = []

    async def handler(conv: uuid.UUID, tenant: uuid.UUID, session: object) -> None:
        calls.append(conv)

    return calls, handler


async def test_process_one_runs_handler_and_releases_lock() -> None:
    payload, conv_id = _payload()
    disp = FakeDispatcher(lock_free=True)
    maker = FakeSessionMaker()
    calls, handler = _recorder()

    await process_one(payload, disp, maker, handler)  # type: ignore[arg-type]

    assert calls == [conv_id]  # handler corrió una vez
    assert maker.opened == 1  # session abierta por item
    assert disp.released == [conv_id]  # lock liberado en finally
    assert disp.enqueued == []  # no se re-encola


async def test_process_one_requeues_when_lock_held() -> None:
    payload, conv_id = _payload()
    disp = FakeDispatcher(lock_free=False)
    maker = FakeSessionMaker()
    calls, handler = _recorder()

    await process_one(payload, disp, maker, handler)  # type: ignore[arg-type]

    assert calls == []  # no procesa
    assert maker.opened == 0  # ni abre session
    assert disp.enqueued == [conv_id]  # re-encolado
    assert disp.released == []  # nunca tomó el lock → no libera


async def test_consume_processes_each_item_once_then_stops() -> None:
    stop = asyncio.Event()
    payload, conv_id = _payload()

    class StoppingDispatcher(FakeDispatcher):
        async def dequeue(self, timeout: int = 0) -> dict[str, str] | None:
            if self.items:
                return self.items.pop(0)
            stop.set()  # cola vacía → cortar el loop
            return None

    disp = StoppingDispatcher([payload])
    maker = FakeSessionMaker()
    calls, handler = _recorder()

    await consume(stop, disp, maker, handler)  # type: ignore[arg-type]

    assert calls == [conv_id]  # procesado exactamente una vez
    assert disp.released == [conv_id]
    assert disp.beats >= 1  # heartbeat refrescado en cada iteración (#246)
