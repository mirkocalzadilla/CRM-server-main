"""Outbound (etapa D): reactivación de leads fríos por reglas, con tope diario y guardas."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from server.config import get_settings
from server.modules.agent.domain.catalog_models import Service
from server.modules.agent.domain.funnel_fsm import FunnelStage
from server.modules.agent.domain.models import (
    Agent,
    AgentInstance,
    AiChatHistory,
    Conversation,
    Product,
)
from server.modules.crm.domain import stages
from server.modules.crm.domain.models import Card, CardService
from server.modules.crm.repositories.board_repository import BoardRepository
from server.modules.crm.seed import seed_crm
from server.modules.outbound.domain.models import OutboundMessage
from server.modules.outbound.domain.settings_models import OutboundSettings, parse_rules
from server.modules.outbound.repositories.opt_out_repository import OptOutRepository
from server.modules.outbound.services.reactivation_service import ReactivationService
from server.shared.timezone import BUSINESS_TZ, to_business_time

SessionFactory = async_sessionmaker[AsyncSession]
# Hoy a las 15:00 La Paz: el tope diario cuenta filas por `created_at` real, así que la
# fecha simulada debe ser la del reloj del test.
NOW = datetime.now(BUSINESS_TZ).replace(hour=15, minute=0, second=0, microsecond=0).astimezone(UTC)


class _FakeSender:
    def __init__(self) -> None:
        self.calls: list[tuple[str, list[str]]] = []

    async def send_template(
        self, to: str, name: str, lang: str, components: list[dict[str, object]]
    ) -> str | None:
        body = next(c for c in components if c["type"] == "body")
        self.calls.append((to, [str(p["text"]) for p in body["parameters"]]))  # type: ignore[union-attr]
        return f"wamid.{uuid.uuid4().hex[:8]}"


class _Org:
    def __init__(self, org_id: uuid.UUID, agent_id: uuid.UUID, instance_id: uuid.UUID) -> None:
        self.org_id, self.agent_id, self.instance_id = org_id, agent_id, instance_id


async def _seed_org(
    session_factory: SessionFactory, *, enabled: bool = True, novelty: str = "abrimos fechas"
) -> _Org:
    org_id = uuid.uuid4()
    async with session_factory() as session:
        await seed_crm(session, org_id)
        if await session.get(Product, "cursos-mirko") is None:
            session.add(Product(slug="cursos-mirko", display_name="Cursos"))
            await session.flush()
        agent = Agent(
            organization_id=org_id,
            product_slug="cursos-mirko",
            display_name="A",
            system_prompt="x",
            model="claude-haiku-4-5-20251001",
        )
        session.add(agent)
        await session.flush()
        instance = AgentInstance(agent_id=agent.id, display_name="WA")
        session.add(instance)
        await session.flush()
        session.add(
            OutboundSettings(
                organization_id=org_id, reactivation_enabled=enabled, novelty_text=novelty
            )
        )
        await session.commit()
        return _Org(org_id, agent.id, instance.id)


async def _lead(
    session_factory: SessionFactory,
    org: _Org,
    wa_id: str,
    *,
    stage: FunnelStage = FunnelStage.ENGAGING,
    last_user_days_ago: int = 10,
    name: str | None = "Sara Siles",
    ai_active: bool = True,
    card_stage: str = stages.IA_ENGAGING,
    service: str | None = None,
) -> uuid.UUID:
    async with session_factory() as session:
        conversation = Conversation(
            instance_id=org.instance_id,
            organization_id=org.org_id,
            external_id=wa_id,
            funnel_stage=stage,
            is_ai_active=ai_active,
            full_name=name,
        )
        session.add(conversation)
        await session.flush()
        board = BoardRepository(session)
        column = await board.get_stage(org.org_id, stages.PIPELINE_IA, card_stage)
        assert column is not None, card_stage
        card = Card(
            organization_id=org.org_id,
            conversation_id=conversation.id,
            stage_id=column.id,
            title=name or "Lead",
        )
        session.add(card)
        await session.flush()
        if service:
            svc = Service(
                organization_id=org.org_id,
                agent_id=org.agent_id,
                slug=service.lower().replace(" ", "-"),
                nombre=service,
                resumen="r",
                precio="100",
                moneda="BOB",
                flujo_cierre="pago_qr",
            )
            session.add(svc)
            await session.flush()
            session.add(
                CardService(
                    organization_id=org.org_id,
                    card_id=card.id,
                    service_id=svc.id,
                    source="captured",
                )
            )
        session.add(
            AiChatHistory(
                agent_id=org.agent_id,
                organization_id=org.org_id,
                thread_id=str(conversation.id),
                session_id=wa_id,
                message={"role": "user", "content": "hola"},
                created_at=NOW - timedelta(days=last_user_days_ago),
            )
        )
        await session.commit()
        return conversation.id


async def _run(session_factory: SessionFactory, sender: _FakeSender, now: datetime = NOW):
    async with session_factory() as session:
        run = await ReactivationService(session, sender).run(now)
        await session.commit()
        return run


def test_rules_parse_tolerantly() -> None:
    rules = parse_rules([{"stages": ["new"], "days": 14}, {"bad": 1}, {"stages": [], "days": 3}])
    assert [(r.stages, r.inactive_days) for r in rules] == [(("new",), 14)]
    assert parse_rules("nope") == []


async def test_cold_engaging_lead_gets_the_template_with_its_service(
    session_factory: SessionFactory,
) -> None:
    org = await _seed_org(session_factory)
    await _lead(session_factory, org, "59170000001", service="Taller de CapCut")
    sender = _FakeSender()
    run = await _run(session_factory, sender)
    assert run.sent == 1
    assert sender.calls == [("59170000001", ["Sara", "Taller de CapCut", "abrimos fechas"])]
    async with session_factory() as session:
        (row,) = (await session.execute(select(OutboundMessage))).scalars().all()
    assert row.purpose == "reactivation" and row.status == "sent"
    assert row.dedupe_key == f"reactivation:{row.conversation_id}:{to_business_time(NOW):%Y-%m-%d}"


async def test_recent_lead_human_takeover_and_new_under_14_days_are_skipped(
    session_factory: SessionFactory,
) -> None:
    org = await _seed_org(session_factory)
    await _lead(session_factory, org, "59170000002", last_user_days_ago=3)  # aún tibio
    await _lead(session_factory, org, "59170000003", ai_active=False)  # lo atiende un humano
    await _lead(session_factory, org, "59170000004", stage=FunnelStage.NEW, last_user_days_ago=10)
    await _lead(session_factory, org, "59170000005", stage=FunnelStage.NEW, last_user_days_ago=20)
    sender = _FakeSender()
    run = await _run(session_factory, sender)
    assert [to for to, _ in sender.calls] == ["59170000005"]
    assert run.sent == 1


async def test_opt_out_disabled_org_and_missing_novelty_send_nothing(
    session_factory: SessionFactory,
) -> None:
    org = await _seed_org(session_factory)
    await _lead(session_factory, org, "59170000006")
    async with session_factory() as session:
        await OptOutRepository(session).add(org.org_id, "59170000006", "keyword")
        await session.commit()
    disabled = await _seed_org(session_factory, enabled=False)
    await _lead(session_factory, disabled, "59170000007")
    blank = await _seed_org(session_factory, novelty="   ")
    await _lead(session_factory, blank, "59170000008")
    sender = _FakeSender()
    run = await _run(session_factory, sender)
    assert sender.calls == [] and run.sent == 0


async def test_daily_cap_and_recontact_window_limit_sends(session_factory: SessionFactory) -> None:
    org = await _seed_org(session_factory)
    async with session_factory() as session:
        cfg = (
            await session.execute(
                select(OutboundSettings).where(OutboundSettings.organization_id == org.org_id)
            )
        ).scalar_one()
        cfg.reactivation_daily_cap = 2
        await session.commit()
    for i in range(3):
        await _lead(session_factory, org, f"5917000001{i}", name=f"Lead {i}")
    sender = _FakeSender()
    first = await _run(session_factory, sender)
    assert first.sent == 2  # tope diario

    later_today = NOW + timedelta(hours=1)
    second = await _run(session_factory, sender, later_today)
    assert second.sent == 0  # el tope ya se consumió hoy

    next_day = NOW + timedelta(days=1)
    third = await _run(session_factory, sender, next_day)
    assert third.sent == 1  # el tercero sale; los dos primeros están dentro del recontacto
    assert len(sender.calls) == 3 and len({to for to, _ in sender.calls}) == 3


async def test_outside_send_hours_does_nothing(session_factory: SessionFactory) -> None:
    org = await _seed_org(session_factory)
    await _lead(session_factory, org, "59170000020")
    sender = _FakeSender()
    late = to_business_time(NOW).replace(hour=22, minute=30).astimezone(UTC)
    run = await _run(session_factory, sender, late)
    assert run.reason == "outside_send_hours" and sender.calls == []


@pytest.fixture(autouse=True)
def _send_hours(monkeypatch: pytest.MonkeyPatch) -> None:
    settings = get_settings()
    monkeypatch.setattr(settings, "outbound_send_hour_start", 7)
    monkeypatch.setattr(settings, "outbound_send_hour_end", 22)
