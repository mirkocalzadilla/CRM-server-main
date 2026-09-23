"""Outbound (etapa C): recordatorios de evento a las entradas vivas, 48 h y 3 h antes."""

from __future__ import annotations

import asyncio
import uuid
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from server.config import get_settings
from server.modules.agent.domain.catalog_models import Service
from server.modules.agent.domain.models import Agent, AgentInstance, Conversation, Product
from server.modules.crm.domain import stages
from server.modules.crm.domain.event_models import Event
from server.modules.crm.domain.models import Card, QrEntry
from server.modules.crm.repositories.board_repository import BoardRepository
from server.modules.crm.seed import seed_crm
from server.modules.outbound.domain.models import OutboundMessage
from server.modules.outbound.domain.schedule import due_reminder_window, is_within_send_hours
from server.modules.outbound.repositories.opt_out_repository import OptOutRepository
from server.modules.outbound.services.event_reminder_service import EventReminderService
from server.modules.outbound.services.outbound_jobs import run_jobs_once, run_outbound_jobs
from server.shared.timezone import BUSINESS_TZ

SessionFactory = async_sessionmaker[AsyncSession]
# 15:00 La Paz on a weekday: inside the 07-22 send window.
NOW = datetime(2026, 10, 1, 15, 0, tzinfo=BUSINESS_TZ).astimezone(UTC)


class _FakeSender:
    def __init__(self) -> None:
        self.calls: list[tuple[str, str, list[str]]] = []

    async def send_template(
        self, to: str, name: str, lang: str, components: list[dict[str, object]]
    ) -> str | None:
        body = next(c for c in components if c["type"] == "body")
        texts = [str(p["text"]) for p in body["parameters"]]  # type: ignore[union-attr]
        self.calls.append((to, name, texts))
        return f"wamid.{uuid.uuid4().hex[:8]}"


async def _seed_event_with_entries(
    session_factory: SessionFactory, *, starts_in: timedelta, leads: list[tuple[str, str]]
) -> tuple[uuid.UUID, uuid.UUID, list[uuid.UUID]]:
    """Returns (org_id, event_id, entry_ids). `leads` = [(wa_id, full_name)]."""
    org_id = uuid.uuid4()
    entry_ids: list[uuid.UUID] = []
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
        service = Service(
            organization_id=org_id,
            agent_id=agent.id,
            slug="taller",
            nombre="Taller",
            resumen="r",
            precio="100",
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
            starts_at=NOW + starts_in,
            location="Av. San Martín 561",
            status="active",
        )
        session.add(event)
        await session.flush()
        stage = await BoardRepository(session).get_stage(
            org_id, stages.PIPELINE_HUMAN, stages.DELIVERED
        )
        assert stage is not None
        for wa_id, full_name in leads:
            conversation = Conversation(
                instance_id=instance.id,
                organization_id=org_id,
                external_id=wa_id,
                full_name=full_name,
            )
            session.add(conversation)
            await session.flush()
            card = Card(
                organization_id=org_id,
                conversation_id=conversation.id,
                stage_id=stage.id,
                title=full_name or "Lead",
            )
            session.add(card)
            await session.flush()
            entry = QrEntry(
                card_id=card.id,
                token=str(uuid.uuid4()),
                qr_ref="qr/x.png",
                event_id=event.id,
            )
            session.add(entry)
            await session.flush()
            entry_ids.append(entry.id)
        await session.commit()
        return org_id, event.id, entry_ids


async def _sent_rows(session_factory: SessionFactory) -> list[OutboundMessage]:
    async with session_factory() as session:
        rows = (await session.execute(select(OutboundMessage))).scalars().all()
        return [r for r in rows if r.status == "sent"]


def test_due_window_picks_the_closest_and_ignores_past_events() -> None:
    windows = (48, 3)
    assert due_reminder_window(NOW + timedelta(hours=72), NOW, windows) is None
    assert due_reminder_window(NOW + timedelta(hours=40), NOW, windows) == 48
    assert due_reminder_window(NOW + timedelta(hours=2), NOW, windows) == 3
    assert due_reminder_window(NOW - timedelta(minutes=1), NOW, windows) is None


def test_send_hours_use_business_timezone() -> None:
    six_am = datetime(2026, 10, 1, 6, 30, tzinfo=BUSINESS_TZ)
    ten_pm = datetime(2026, 10, 1, 22, 0, tzinfo=BUSINESS_TZ)
    assert is_within_send_hours(NOW, 7, 22)
    assert not is_within_send_hours(six_am.astimezone(UTC), 7, 22)
    assert not is_within_send_hours(ten_pm.astimezone(UTC), 7, 22)


async def test_48h_reminder_goes_once_to_each_live_entry(session_factory: SessionFactory) -> None:
    _, _, entry_ids = await _seed_event_with_entries(
        session_factory,
        starts_in=timedelta(hours=40),
        leads=[("59170000001", "Sara Siles"), ("59170000002", None)],  # type: ignore[list-item]
    )
    sender = _FakeSender()
    async with session_factory() as session:
        first = await EventReminderService(session, sender).run(NOW)
        second = await EventReminderService(session, sender).run(NOW)  # mismo tick: no repite
        await session.commit()

    assert (first.sent, second.sent) == (2, 0)
    by_phone = {to: texts for to, _, texts in sender.calls}
    assert by_phone["59170000001"] == [
        "Sara",
        "Taller de CapCut",
        "03/10/2026",
        "07:00",
        "Av. San Martín 561",
    ]
    assert by_phone["59170000002"][0] == "de nuevo"
    rows = await _sent_rows(session_factory)
    assert len(rows) == 2
    assert {r.dedupe_key.split(":")[2] for r in rows if r.dedupe_key} == {str(e) for e in entry_ids}
    assert all(r.dedupe_key and r.dedupe_key.endswith(":48h") for r in rows)


async def test_late_entry_gets_only_the_3h_reminder(session_factory: SessionFactory) -> None:
    await _seed_event_with_entries(
        session_factory, starts_in=timedelta(hours=2), leads=[("59170000003", "Ana")]
    )
    sender = _FakeSender()
    async with session_factory() as session:
        run = await EventReminderService(session, sender).run(NOW)
        await session.commit()
    assert run.sent == 1
    rows = await _sent_rows(session_factory)
    assert rows[0].dedupe_key is not None and rows[0].dedupe_key.endswith(":3h")


async def test_nothing_goes_out_outside_send_hours(session_factory: SessionFactory) -> None:
    await _seed_event_with_entries(
        session_factory, starts_in=timedelta(hours=10), leads=[("59170000004", "Ana")]
    )
    sender = _FakeSender()
    late = datetime(2026, 10, 1, 23, 30, tzinfo=BUSINESS_TZ).astimezone(UTC)
    async with session_factory() as session:
        run = await EventReminderService(session, sender).run(late)
    assert run.reason == "outside_send_hours" and sender.calls == []


async def test_revoked_and_opted_out_entries_are_skipped(session_factory: SessionFactory) -> None:
    org_id, _, entry_ids = await _seed_event_with_entries(
        session_factory,
        starts_in=timedelta(hours=30),
        leads=[("59170000005", "Revocada"), ("59170000006", "Baja")],
    )
    async with session_factory() as session:
        revoked = await session.get(QrEntry, entry_ids[0])
        assert revoked is not None
        revoked.revoked_at = NOW
        await OptOutRepository(session).add(org_id, "59170000006", "keyword")
        await session.commit()
    sender = _FakeSender()
    async with session_factory() as session:
        run = await EventReminderService(session, sender).run(NOW)
        await session.commit()
    assert (run.sent, run.skipped) == (0, 1) and sender.calls == []


async def test_job_runner_isolates_failures_and_stops_on_signal(
    session_factory: SessionFactory,
) -> None:
    calls: list[str] = []

    async def ok(session: AsyncSession, sender: object) -> None:
        calls.append("ok")

    async def boom(session: AsyncSession, sender: object) -> None:
        raise RuntimeError("job roto")

    outcome = await run_jobs_once(session_factory, _FakeSender(), {"boom": boom, "ok": ok})
    assert outcome == {"boom": False, "ok": True} and calls == ["ok"]

    stop = asyncio.Event()
    stop.set()
    await asyncio.wait_for(
        run_outbound_jobs(stop, session_factory, _FakeSender(), 1, {"ok": ok}), timeout=2
    )


@pytest.fixture(autouse=True)
def _default_windows(monkeypatch: pytest.MonkeyPatch) -> None:
    settings = get_settings()
    monkeypatch.setattr(settings, "outbound_reminder_hours", "48,3")
    monkeypatch.setattr(settings, "outbound_send_hour_start", 7)
    monkeypatch.setattr(settings, "outbound_send_hour_end", 22)
