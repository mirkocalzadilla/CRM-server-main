"""SSE del CRM (slice 2b): `GET /crm/events?token=` + shape unificado de `card_moved`.

`httpx.ASGITransport` consume el body completo antes de devolver la respuesta, así
que los fakes de `subscribe` terminan solos (el stream corta y se asierta el body).
El ciclo pubsub real se prueba contra un FakePubSub; el e2e con Redis vivo
(curl -N + CLIENT LIST) va por runbook.
"""

from __future__ import annotations

import asyncio
import json
import uuid
from collections.abc import AsyncGenerator

import pytest
from httpx import AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

import server.modules.crm.api.router as crm_router
import server.shared.pubsub as pubsub_module
from server.modules.core.services.tenant_service import TenantService
from server.modules.crm.domain.models import Card, Pipeline, Stage, StageStatus
from server.modules.crm.services.board_service import BoardService
from server.shared.pubsub import card_moved_event, subscribe

API = "/api/v1"
ADMIN_EMAIL = "admin@example.com"
ADMIN_PASSWORD = "admin-secret"
TENANT_SLUG = "acme"


async def _register(client: AsyncClient) -> str:
    response = await client.post(
        f"{API}/auth/register",
        json={
            "email": ADMIN_EMAIL,
            "password": ADMIN_PASSWORD,
            "full_name": "Admin",
            "tenant_name": "Acme",
            "tenant_slug": TENANT_SLUG,
        },
    )
    assert response.status_code == 201, response.text
    token: str = response.json()["access_token"]
    return token


async def _tenant_id(session_factory: async_sessionmaker[AsyncSession]) -> uuid.UUID:
    async with session_factory() as session:
        tenant = await TenantService(session).get_by_slug(TENANT_SLUG)
        return tenant.id


# --- auth ---


async def test_events_without_token_is_401(client: AsyncClient) -> None:
    response = await client.get(f"{API}/crm/events")
    assert response.status_code == 401


async def test_events_with_invalid_token_is_401(client: AsyncClient) -> None:
    response = await client.get(f"{API}/crm/events", params={"token": "no-es-un-jwt"})
    assert response.status_code == 401


# --- stream ---


async def test_stream_emits_events_from_tenant_channel(
    client: AsyncClient,
    session_factory: async_sessionmaker[AsyncSession],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    payload = json.dumps({"type": "handoff", "conversation_id": str(uuid.uuid4())})
    channels: list[str] = []

    async def _fake_subscribe(channel: str) -> AsyncGenerator[str, None]:
        channels.append(channel)
        yield payload

    monkeypatch.setattr(crm_router, "subscribe", _fake_subscribe)
    token = await _register(client)

    response = await client.get(f"{API}/crm/events", params={"token": token})

    assert response.status_code == 200, response.text
    assert response.headers["content-type"].startswith("text/event-stream")
    assert response.text == f"data: {payload}\n\n"
    # Tenant-scoped: se suscribe únicamente al canal del tenant del token.
    assert channels == [f"crm:events:{await _tenant_id(session_factory)}"]


async def test_stream_sends_heartbeat_when_idle(
    client: AsyncClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def _quiet_subscribe(channel: str) -> AsyncGenerator[str, None]:
        await asyncio.sleep(0.05)
        return
        yield ""  # generador async vacío (nunca llega)

    monkeypatch.setattr(crm_router, "HEARTBEAT_SECONDS", 0.01)
    monkeypatch.setattr(crm_router, "subscribe", _quiet_subscribe)
    token = await _register(client)

    response = await client.get(f"{API}/crm/events", params={"token": token})

    assert response.status_code == 200
    assert ": ping\n\n" in response.text
    assert "data:" not in response.text


async def test_disconnect_cancels_subscription(monkeypatch: pytest.MonkeyPatch) -> None:
    released = asyncio.Event()

    async def _blocking_subscribe(channel: str) -> AsyncGenerator[str, None]:
        try:
            await asyncio.sleep(3600)
            yield ""
        finally:
            released.set()

    monkeypatch.setattr(crm_router, "HEARTBEAT_SECONDS", 0.01)
    monkeypatch.setattr(crm_router, "subscribe", _blocking_subscribe)

    stream = crm_router._event_stream(uuid.uuid4())
    assert await anext(stream) == ": ping\n\n"  # conectado, sin tráfico
    await stream.aclose()  # corte del cliente

    await asyncio.wait_for(released.wait(), timeout=1)


# --- subscribe (ciclo pubsub) ---


class _FakePubSub:
    def __init__(self) -> None:
        self.messages: asyncio.Queue[dict[str, object]] = asyncio.Queue()
        self.subscribed: list[str] = []
        self.unsubscribed: list[str] = []
        self.closed = False

    async def subscribe(self, channel: str) -> None:
        self.subscribed.append(channel)

    async def get_message(
        self, ignore_subscribe_messages: bool = False, timeout: float = 0.0
    ) -> dict[str, object] | None:
        try:
            return await asyncio.wait_for(self.messages.get(), timeout)
        except TimeoutError:
            return None

    async def unsubscribe(self, channel: str) -> None:
        self.unsubscribed.append(channel)

    async def aclose(self) -> None:
        self.closed = True


class _FakeRedis:
    def __init__(self, fake_pubsub: _FakePubSub) -> None:
        self._pubsub = fake_pubsub

    def pubsub(self) -> _FakePubSub:
        return self._pubsub


async def test_subscribe_yields_messages_and_releases_redis(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fake = _FakePubSub()
    monkeypatch.setattr(pubsub_module, "redis_client", _FakeRedis(fake))

    await fake.messages.put({"type": "subscribe", "data": 1})  # confirmación: se filtra
    await fake.messages.put({"type": "message", "data": '{"type":"handoff"}'})

    events = subscribe("crm:events:t1")
    assert await anext(events) == '{"type":"handoff"}'
    await events.aclose()

    assert fake.subscribed == ["crm:events:t1"]
    assert fake.unsubscribed == ["crm:events:t1"]
    assert fake.closed


# --- shape unificado de card_moved ---


def test_card_moved_event_has_unified_shape() -> None:
    card_id, conversation_id = uuid.uuid4(), uuid.uuid4()
    event = card_moved_event(
        card_id=card_id, stage="Nuevo", conversation_id=conversation_id, pipeline_kind="ia"
    )
    assert event == {
        "type": "card_moved",
        "card_id": str(card_id),
        "stage": "Nuevo",
        "conversation_id": str(conversation_id),
        "pipeline_kind": "ia",
    }


class _RecordingPublisher:
    def __init__(self) -> None:
        self.published: list[tuple[str, dict[str, object]]] = []

    async def publish(self, channel: str, payload: dict[str, object]) -> None:
        self.published.append((channel, payload))


async def test_move_card_publishes_unified_card_moved(
    client: AsyncClient, session_factory: async_sessionmaker[AsyncSession]
) -> None:
    await _register(client)
    org_id = await _tenant_id(session_factory)
    conversation_id = uuid.uuid4()  # FK no exigida en el SQLite de tests

    async with session_factory() as session:
        session.add(StageStatus(code="open", name="Abierto"))
        pipeline = Pipeline(organization_id=org_id, kind="ia", name="IA", position=0)
        session.add(pipeline)
        await session.flush()
        origin = Stage(pipeline_id=pipeline.id, name="Nuevo", position=0, status_code="open")
        target = Stage(pipeline_id=pipeline.id, name="Calificado", position=1, status_code="open")
        session.add_all([origin, target])
        await session.flush()
        card = Card(
            organization_id=org_id,
            conversation_id=conversation_id,
            stage_id=origin.id,
            title="Lead",
        )
        session.add(card)
        await session.commit()
        card_id, target_id = card.id, target.id

    recorder = _RecordingPublisher()
    async with session_factory() as session:
        service = BoardService(session=session, publisher=recorder)  # type: ignore[arg-type]
        moved = await service.move_card(card_id, target_id, uuid.uuid4(), org_id)

    assert moved is not None
    assert recorder.published == [
        (
            f"crm:events:{org_id}",
            {
                "type": "card_moved",
                "card_id": str(card_id),
                "stage": "Calificado",
                "conversation_id": str(conversation_id),
                "pipeline_kind": "ia",
            },
        )
    ]
