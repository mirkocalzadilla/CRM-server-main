"""API de M-Outbound (etapa E): historial, configuración, bajas y envíos manuales desde una card."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta

import pytest
from httpx import AsyncClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from server.modules.agent.domain.catalog_models import Service
from server.modules.agent.domain.models import Agent, AgentInstance, Conversation, Product
from server.modules.agent.services.whatsapp_service import WhatsAppSender
from server.modules.core.domain.models import Tenant
from server.modules.crm.domain import stages
from server.modules.crm.domain.event_models import Event
from server.modules.crm.domain.models import Card, QrEntry
from server.modules.crm.repositories.board_repository import BoardRepository
from server.modules.crm.seed import seed_crm
from server.modules.outbound.domain.models import OutboundMessage

API = "/api/v1"
SessionFactory = async_sessionmaker[AsyncSession]


def _auth(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


async def _register(client: AsyncClient, slug: str) -> str:
    response = await client.post(
        f"{API}/auth/register",
        json={
            "email": f"owner@{slug}.com",
            "password": "secret-pass",
            "full_name": "Owner",
            "tenant_name": slug.capitalize(),
            "tenant_slug": slug,
        },
    )
    assert response.status_code == 201, response.text
    return str(response.json()["access_token"])


async def _org_id(session_factory: SessionFactory, slug: str) -> uuid.UUID:
    async with session_factory() as session:
        tenant = (await session.execute(select(Tenant).where(Tenant.slug == slug))).scalar_one()
        return tenant.id


async def _seed_card(
    session_factory: SessionFactory, org_id: uuid.UUID, *, with_entry: bool
) -> uuid.UUID:
    async with session_factory() as session:
        await seed_crm(session, org_id)
        if await session.get(Product, "cursos-mirko") is None:
            session.add(Product(slug="cursos-mirko", display_name="Cursos"))
            await session.flush()
        agent = (
            await session.execute(select(Agent).where(Agent.organization_id == org_id))
        ).scalar_one_or_none()
        if agent is None:  # a second card for the same org reuses its agent/instance
            agent = Agent(
                organization_id=org_id,
                product_slug="cursos-mirko",
                display_name="A",
                system_prompt="x",
                model="claude-haiku-4-5-20251001",
            )
            session.add(agent)
            await session.flush()
            session.add(AgentInstance(agent_id=agent.id, display_name="WA"))
            await session.flush()
        instance = (
            await session.execute(select(AgentInstance).where(AgentInstance.agent_id == agent.id))
        ).scalar_one()
        conversation = Conversation(
            instance_id=instance.id,
            organization_id=org_id,
            external_id="59170000077",
            full_name="Sara Siles",
        )
        session.add(conversation)
        await session.flush()
        stage = await BoardRepository(session).get_stage(
            org_id, stages.PIPELINE_IA, stages.IA_ENGAGING
        )
        assert stage is not None
        card = Card(
            organization_id=org_id,
            conversation_id=conversation.id,
            stage_id=stage.id,
            title="Sara Siles",
        )
        session.add(card)
        await session.flush()
        if with_entry:
            service = Service(
                organization_id=org_id,
                agent_id=agent.id,
                slug=f"taller-{uuid.uuid4().hex[:6]}",
                nombre="Taller",
                resumen="r",
                precio="1",
                moneda="BOB",
                flujo_cierre="pago_qr",
                modality="presencial",
            )
            session.add(service)
            await session.flush()
            event = Event(
                organization_id=org_id,
                service_id=service.id,
                nombre="Taller de CapCut",
                starts_at=datetime.now(UTC) + timedelta(days=3),
                location="Sede",
                status="active",
            )
            session.add(event)
            await session.flush()
            session.add(
                QrEntry(
                    card_id=card.id, token=str(uuid.uuid4()), qr_ref="qr/x.png", event_id=event.id
                )
            )
        await session.commit()
        return card.id


@pytest.fixture
def fake_meta(monkeypatch: pytest.MonkeyPatch) -> list[tuple[str, str]]:
    """El router usa el `WhatsAppSender` real: se reemplaza solo `send_template`."""
    calls: list[tuple[str, str]] = []

    async def _send_template(
        self: WhatsAppSender, to: str, name: str, lang: str, components: list[dict[str, object]]
    ) -> str | None:
        calls.append((to, name))
        return f"wamid.{len(calls)}"

    monkeypatch.setattr(WhatsAppSender, "send_template", _send_template)
    return calls


async def test_settings_default_and_enable_requires_novelty(client: AsyncClient) -> None:
    h = _auth(await _register(client, "acme"))
    got = await client.get(f"{API}/outbound/settings", headers=h)
    assert got.status_code == 200, got.text
    body = got.json()
    assert body["reactivation_enabled"] is False and body["reactivation_daily_cap"] == 100
    assert body["reactivation_rules"][0] == {"stages": ["engaging", "qualified"], "days": 7}

    denied = await client.put(
        f"{API}/outbound/settings", json={"reactivation_enabled": True}, headers=h
    )
    assert denied.status_code == 400

    ok = await client.put(
        f"{API}/outbound/settings",
        json={
            "reactivation_enabled": True,
            "novelty_text": "  abrimos fechas en octubre  ",
            "reactivation_daily_cap": 50,
            "reactivation_rules": [{"stages": ["new"], "days": 21}],
        },
        headers=h,
    )
    assert ok.status_code == 200, ok.text
    saved = ok.json()
    assert saved["reactivation_enabled"] is True
    assert saved["novelty_text"] == "abrimos fechas en octubre"
    assert saved["reactivation_daily_cap"] == 50
    assert saved["reactivation_rules"] == [{"stages": ["new"], "days": 21}]

    bad_stage = await client.put(
        f"{API}/outbound/settings",
        json={"reactivation_rules": [{"stages": ["marte"], "days": 3}]},
        headers=h,
    )
    assert bad_stage.status_code == 422


async def test_opt_outs_and_messages_are_tenant_scoped(
    client: AsyncClient, session_factory: SessionFactory
) -> None:
    h_a = _auth(await _register(client, "alfa"))
    h_b = _auth(await _register(client, "beta"))
    org_a = await _org_id(session_factory, "alfa")

    created = await client.post(
        f"{API}/outbound/opt-outs", json={"wa_id": "59170000001"}, headers=h_a
    )
    assert created.status_code == 201
    assert [
        o["wa_id"] for o in (await client.get(f"{API}/outbound/opt-outs", headers=h_a)).json()
    ] == ["59170000001"]
    assert (await client.get(f"{API}/outbound/opt-outs", headers=h_b)).json() == []

    async with session_factory() as session:
        session.add(
            OutboundMessage(
                organization_id=org_a,
                wa_id="59170000001",
                template_name="recordatorio_evento",
                purpose="event_reminder",
                status="delivered",
                wamid="wamid.x",
                rendered_text="Hola Sara!",
            )
        )
        await session.commit()

    page = (await client.get(f"{API}/outbound/messages?purpose=event_reminder", headers=h_a)).json()
    assert [m["template_name"] for m in page["items"]] == ["recordatorio_evento"]
    assert page["items"][0]["status"] == "delivered"
    assert (await client.get(f"{API}/outbound/messages", headers=h_b)).json()["items"] == []


async def test_manual_reactivation_needs_novelty_then_sends(
    client: AsyncClient, session_factory: SessionFactory, fake_meta: list[tuple[str, str]]
) -> None:
    h = _auth(await _register(client, "gamma"))
    card_id = await _seed_card(
        session_factory, await _org_id(session_factory, "gamma"), with_entry=False
    )

    first = await client.post(f"{API}/outbound/cards/{card_id}/reactivate", headers=h)
    assert first.status_code == 200 and first.json() == {"sent": False, "reason": "sin_novedad"}

    await client.put(f"{API}/outbound/settings", json={"novelty_text": "hay fechas"}, headers=h)
    second = await client.post(f"{API}/outbound/cards/{card_id}/reactivate", headers=h)
    assert second.status_code == 200 and second.json()["sent"] is True
    assert fake_meta == [("59170000077", "reactivacion_leads")]

    history = (await client.get(f"{API}/outbound/cards/{card_id}/messages", headers=h)).json()
    assert [m["purpose"] for m in history] == ["reactivation"]

    missing = await client.post(f"{API}/outbound/cards/{uuid.uuid4()}/reactivate", headers=h)
    assert missing.status_code == 404


async def test_manual_reminder_requires_a_live_entry(
    client: AsyncClient, session_factory: SessionFactory, fake_meta: list[tuple[str, str]]
) -> None:
    h = _auth(await _register(client, "delta"))
    org_id = await _org_id(session_factory, "delta")
    without = await _seed_card(session_factory, org_id, with_entry=False)
    none = await client.post(f"{API}/outbound/cards/{without}/remind", headers=h)
    assert none.json() == {"sent": False, "reason": "sin_entrada_vigente"}

    with_entry = await _seed_card(session_factory, org_id, with_entry=True)
    ok = await client.post(f"{API}/outbound/cards/{with_entry}/remind", headers=h)
    assert ok.status_code == 200 and ok.json()["sent"] is True
    assert fake_meta == [("59170000077", "recordatorio_evento")]
