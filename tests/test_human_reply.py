"""Reply humano desde el CRM (B3): POST /crm/cards/{id}/send.

Seed completo del chain (product→agent→instance→conversation + pipeline/stage/card)
sobre SQLite y se ejerce el endpoint con auth real. La llamada a Meta se mockea
(no se hace HTTP de salida en tests); la atomicidad enviar→persistir y el gate
`is_ai_active` se validan acá.
"""

from __future__ import annotations

import uuid

import pytest
from httpx import AsyncClient
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from server.modules.agent.domain.models import (
    Agent,
    AgentInstance,
    AppChatHistory,
    Conversation,
    Product,
)
from server.modules.agent.services.whatsapp_service import WhatsAppSender
from server.modules.core.services.tenant_service import TenantService
from server.modules.crm.domain.models import Card, Pipeline, Stage, StageStatus

API = "/api/v1"
ADMIN_EMAIL = "admin@example.com"
ADMIN_PASSWORD = "admin-secret"
TENANT_SLUG = "acme"
LEAD_WA_ID = "59170000000"


def _auth(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


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


async def _seed_card(
    session_factory: async_sessionmaker[AsyncSession], *, is_ai_active: bool
) -> str:
    """Crea el chain mínimo bajo el tenant `acme` y devuelve el card_id."""
    async with session_factory() as session:
        tenant = await TenantService(session).get_by_slug(TENANT_SLUG)
        org_id = tenant.id

        session.add(StageStatus(code="open", name="Abierto"))
        session.add(Product(slug="cursos-mirko", display_name="Cursos Mirko"))
        agent = Agent(
            organization_id=org_id,
            product_slug="cursos-mirko",
            display_name="Asistente",
            system_prompt="x",
            model="claude-haiku-4-5-20251001",
        )
        session.add(agent)
        await session.flush()
        instance = AgentInstance(agent_id=agent.id, display_name="WA")
        session.add(instance)
        await session.flush()
        conversation = Conversation(
            instance_id=instance.id,
            organization_id=org_id,
            external_id=LEAD_WA_ID,
            is_ai_active=is_ai_active,
        )
        session.add(conversation)
        pipeline = Pipeline(organization_id=org_id, kind="human", name="Gestión", position=0)
        session.add(pipeline)
        await session.flush()
        stage = Stage(pipeline_id=pipeline.id, name="Entrada", position=0, status_code="open")
        session.add(stage)
        await session.flush()
        card = Card(
            organization_id=org_id,
            conversation_id=conversation.id,
            stage_id=stage.id,
            title="Lead",
        )
        session.add(card)
        await session.commit()
        return str(card.id)


async def _count_app_messages(session_factory: async_sessionmaker[AsyncSession]) -> int:
    async with session_factory() as session:
        result = await session.execute(select(func.count()).select_from(AppChatHistory))
        return int(result.scalar_one())


async def test_send_persists_and_returns_thread_message(
    client: AsyncClient,
    session_factory: async_sessionmaker[AsyncSession],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    sent: list[tuple[str, str]] = []

    async def _fake_send_text(self: WhatsAppSender, to: str, body: str) -> None:
        sent.append((to, body))

    monkeypatch.setattr(WhatsAppSender, "send_text", _fake_send_text)

    token = await _register(client)
    card_id = await _seed_card(session_factory, is_ai_active=False)

    response = await client.post(
        f"{API}/crm/cards/{card_id}/send",
        json={"text": "Hola, ya validamos tu pago"},
        headers=_auth(token),
    )

    assert response.status_code == 200, response.text
    body = response.json()
    assert body["sender"] == "human"
    assert body["text"] == "Hola, ya validamos tu pago"
    assert "at" in body
    assert sent == [(LEAD_WA_ID, "Hola, ya validamos tu pago")]
    assert await _count_app_messages(session_factory) == 1


async def test_send_rejected_when_ai_active(
    client: AsyncClient,
    session_factory: async_sessionmaker[AsyncSession],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def _fake_send_text(self: WhatsAppSender, to: str, body: str) -> None:
        raise AssertionError("no debe enviarse con el agente activo")

    monkeypatch.setattr(WhatsAppSender, "send_text", _fake_send_text)

    token = await _register(client)
    card_id = await _seed_card(session_factory, is_ai_active=True)

    response = await client.post(
        f"{API}/crm/cards/{card_id}/send",
        json={"text": "intento"},
        headers=_auth(token),
    )

    assert response.status_code == 400, response.text
    assert await _count_app_messages(session_factory) == 0


async def test_send_404_when_card_missing(
    client: AsyncClient,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def _fake_send_text(self: WhatsAppSender, to: str, body: str) -> None:
        raise AssertionError("no debe enviarse a una card inexistente")

    monkeypatch.setattr(WhatsAppSender, "send_text", _fake_send_text)

    token = await _register(client)
    response = await client.post(
        f"{API}/crm/cards/{uuid.uuid4()}/send",
        json={"text": "hola"},
        headers=_auth(token),
    )
    assert response.status_code == 404, response.text
