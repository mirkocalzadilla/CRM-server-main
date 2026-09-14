"""Tests del Dispatcher (puro, sin Redis real). Ver SPECS_MVP §"M1 en detalle"."""

from __future__ import annotations

import json
import uuid

from server.shared.dispatcher import (
    DISPATCH_LIST,
    JOB_AGENT_TURN,
    JOB_RECEIPT_VISION,
    LOCK_KEY_PREFIX,
    VISION_LIST,
    VISION_LOCK_PREFIX,
    Dispatcher,
)


class FakeRedis:
    """In-memory stand-in for the slice of the Redis async client Dispatcher uses.

    Con una cola por nombre de lista: los turnos del agente y la validación de
    comprobantes usan listas distintas para que un comprobante lento no retrase las
    respuestas de todos los leads."""

    def __init__(self) -> None:
        self.queues: dict[str, list[str]] = {}
        self.keys: dict[str, str] = {}

    @property
    def queue(self) -> list[str]:
        """La cola de turnos del agente (la que miran los tests de siempre)."""
        return self.queues.setdefault(DISPATCH_LIST, [])

    async def lpush(self, name: str, value: str) -> int:
        queue = self.queues.setdefault(name, [])
        queue.insert(0, value)
        return len(queue)

    async def brpop(self, keys: list[str], timeout: int = 0) -> tuple[str, str] | None:
        queue = self.queues.setdefault(keys[0], [])
        if not queue:
            return None
        return keys[0], queue.pop()

    async def set(
        self, name: str, value: str, nx: bool = False, ex: int | None = None
    ) -> bool | None:
        if nx and name in self.keys:
            return None
        self.keys[name] = value
        return True

    async def delete(self, *names: str) -> int:
        return sum(1 for name in names if self.keys.pop(name, None) is not None)


def _dispatcher() -> tuple[Dispatcher, FakeRedis]:
    fake = FakeRedis()
    return Dispatcher(fake), fake  # type: ignore[arg-type]


async def test_enqueue_pushes_contract_payload_to_dispatch_list() -> None:
    dispatcher, fake = _dispatcher()
    conv_id, tenant_id = uuid.uuid4(), uuid.uuid4()

    await dispatcher.enqueue(conversation_id=conv_id, tenant_id=tenant_id)

    assert len(fake.queue) == 1
    assert json.loads(fake.queue[0]) == {
        "conversation_id": str(conv_id),
        "tenant_id": str(tenant_id),
        "kind": JOB_AGENT_TURN,
    }


async def test_dequeue_returns_decoded_payload_fifo() -> None:
    dispatcher, _ = _dispatcher()
    conv_id, tenant_id = uuid.uuid4(), uuid.uuid4()
    await dispatcher.enqueue(conversation_id=conv_id, tenant_id=tenant_id)

    payload = await dispatcher.dequeue()

    assert payload == {
        "conversation_id": str(conv_id),
        "tenant_id": str(tenant_id),
        "kind": JOB_AGENT_TURN,
    }


async def test_dequeue_returns_none_when_queue_is_empty() -> None:
    dispatcher, _ = _dispatcher()

    assert await dispatcher.dequeue(timeout=1) is None
    assert DISPATCH_LIST  # contract constant stays referenced/used by the worker too


async def test_lock_blocks_concurrent_acquire_until_released() -> None:
    dispatcher, fake = _dispatcher()
    conv_id = uuid.uuid4()

    assert await dispatcher.acquire_lock(conv_id) is True
    assert await dispatcher.acquire_lock(conv_id) is False  # already held — re-encolar y seguir
    assert f"{LOCK_KEY_PREFIX}{conv_id}" in fake.keys

    await dispatcher.release_lock(conv_id)

    assert f"{LOCK_KEY_PREFIX}{conv_id}" not in fake.keys
    assert await dispatcher.acquire_lock(conv_id) is True


async def test_vision_uses_its_own_queue() -> None:
    """La validación de comprobantes no comparte cola con los turnos: una llamada de
    visión de varios segundos no puede retrasar las respuestas de otros leads."""
    dispatcher, fake = _dispatcher()
    conv_id, tenant_id = uuid.uuid4(), uuid.uuid4()

    await dispatcher.enqueue_vision(conversation_id=conv_id, tenant_id=tenant_id, wamid="wamid.ABC")

    assert fake.queues.get(DISPATCH_LIST, []) == []  # la cola de turnos queda intacta
    assert len(fake.queues[VISION_LIST]) == 1
    assert json.loads(fake.queues[VISION_LIST][0]) == {
        "conversation_id": str(conv_id),
        "tenant_id": str(tenant_id),
        "wamid": "wamid.ABC",
        "kind": JOB_RECEIPT_VISION,
    }


async def test_dequeue_vision_reads_only_the_vision_queue() -> None:
    dispatcher, _ = _dispatcher()
    conv_id, tenant_id = uuid.uuid4(), uuid.uuid4()
    await dispatcher.enqueue(conversation_id=conv_id, tenant_id=tenant_id)
    await dispatcher.enqueue_vision(conversation_id=conv_id, tenant_id=tenant_id, wamid="wamid.ABC")

    payload = await dispatcher.dequeue_vision()

    assert payload is not None
    assert payload["wamid"] == "wamid.ABC"
    assert await dispatcher.dequeue_vision() is None  # no toma del otro
    assert await dispatcher.dequeue() is not None  # el turno sigue en su cola


async def test_vision_lock_is_per_message_not_per_conversation() -> None:
    """Dos jobs del mismo comprobante no corren a la vez, pero un comprobante no
    bloquea el turno del agente de esa conversación."""
    dispatcher, fake = _dispatcher()
    conv_id = uuid.uuid4()

    assert await dispatcher.acquire_vision_lock("wamid.ABC") is True
    assert await dispatcher.acquire_vision_lock("wamid.ABC") is False
    assert await dispatcher.acquire_vision_lock("wamid.OTRO") is True
    # El lock de conversación es independiente del de comprobante.
    assert await dispatcher.acquire_lock(conv_id) is True
    assert f"{VISION_LOCK_PREFIX}wamid.ABC" in fake.keys
    assert f"{LOCK_KEY_PREFIX}{conv_id}" in fake.keys

    await dispatcher.release_vision_lock("wamid.ABC")
    assert f"{VISION_LOCK_PREFIX}wamid.ABC" not in fake.keys
    assert await dispatcher.acquire_vision_lock("wamid.ABC") is True
