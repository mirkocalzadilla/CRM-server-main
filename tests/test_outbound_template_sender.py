"""Outbound (etapa A): envíos de plantilla registrados, idempotentes y respetuosos de la baja."""

from __future__ import annotations

import uuid

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from server.modules.agent.domain.models import AiChatHistory
from server.modules.outbound.domain.models import (
    PURPOSE_EVENT_REMINDER,
    PURPOSE_REACTIVATION,
    STATUS_FAILED,
    STATUS_SENT,
    STATUS_SKIPPED,
    OutboundMessage,
)
from server.modules.outbound.domain.templates import (
    ENTRY_QR_READY,
    REACTIVACION_LEADS,
    RECORDATORIO_EVENTO,
    build_components,
)
from server.modules.outbound.repositories.opt_out_repository import OptOutRepository
from server.modules.outbound.services.template_sender import SendRequest, TemplateSender

ORG = uuid.uuid4()
AGENT = uuid.uuid4()
CONV = uuid.uuid4()
WA = "59170000001"


class _FakeSender:
    def __init__(self, *, fail: bool = False) -> None:
        self.calls: list[tuple[str, str, list[dict[str, object]]]] = []
        self._fail = fail

    async def send_template(
        self, to: str, name: str, lang: str, components: list[dict[str, object]]
    ) -> str | None:
        self.calls.append((to, name, components))
        if self._fail:
            raise RuntimeError("meta 500")
        return f"wamid.{len(self.calls)}"


def _req(**overrides: object) -> SendRequest:
    base: dict[str, object] = {
        "organization_id": ORG,
        "wa_id": WA,
        "template": RECORDATORIO_EVENTO,
        "variables": ["Sara", "la clase", "15 de octubre", "19:00", "Av. San Martín 561"],
        "purpose": PURPOSE_EVENT_REMINDER,
        "conversation_id": CONV,
        "agent_id": AGENT,
    }
    base.update(overrides)
    return SendRequest(**base)  # type: ignore[arg-type]


async def _rows(session_factory: async_sessionmaker[AsyncSession]) -> list[OutboundMessage]:
    async with session_factory() as session:
        result = await session.execute(select(OutboundMessage))
        return list(result.scalars().all())


async def test_send_records_row_with_wamid_and_mirrors_thread(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    sender = _FakeSender()
    async with session_factory() as session:
        result = await TemplateSender(session, sender).send(_req(dedupe_key="rem:1"))
        await session.commit()

    assert result.sent
    (row,) = await _rows(session_factory)
    assert row.status == STATUS_SENT
    assert row.wamid == "wamid.1"
    assert row.dedupe_key == "rem:1"
    assert row.rendered_text.startswith("Hola Sara! Te recuerdo que la clase es el 15 de octubre")
    to, name, components = sender.calls[0]
    assert (to, name) == (WA, "recordatorio_evento")
    assert components[0]["type"] == "body"
    async with session_factory() as session:
        mirrored = (await session.execute(select(AiChatHistory))).scalars().all()
    assert len(mirrored) == 1
    assert mirrored[0].message["kind"] == "template"
    assert mirrored[0].thread_id == str(CONV)


async def test_dedupe_key_makes_second_send_a_noop(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    sender = _FakeSender()
    async with session_factory() as session:
        svc = TemplateSender(session, sender)
        first = await svc.send(_req(dedupe_key="rem:dup"))
        second = await svc.send(_req(dedupe_key="rem:dup"))
        await session.commit()

    assert first.sent and second.status == STATUS_SKIPPED and second.reason == "duplicate"
    assert len(sender.calls) == 1
    assert len(await _rows(session_factory)) == 1


async def test_opted_out_phone_is_skipped_and_recorded(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    sender = _FakeSender()
    async with session_factory() as session:
        await OptOutRepository(session).add(ORG, WA, "keyword")
        result = await TemplateSender(session, sender).send(
            _req(
                template=REACTIVACION_LEADS,
                purpose=PURPOSE_REACTIVATION,
                variables=["Sara", "el taller", "hay fechas nuevas"],
            )
        )
        await session.commit()

    assert result.status == STATUS_SKIPPED and result.reason == "opt_out"
    assert sender.calls == []
    (row,) = await _rows(session_factory)
    assert row.status == STATUS_SKIPPED and row.error_detail == "opt_out"


async def test_meta_error_leaves_failed_row_with_reason(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    async with session_factory() as session:
        result = await TemplateSender(session, _FakeSender(fail=True)).send(_req())
        await session.commit()

    assert result.status == STATUS_FAILED and result.reason == "meta_error"
    (row,) = await _rows(session_factory)
    assert (
        row.status == STATUS_FAILED and row.wamid is None and "meta 500" in (row.error_detail or "")
    )


def test_build_components_validates_variables_and_header() -> None:
    with pytest.raises(ValueError):
        build_components(RECORDATORIO_EVENTO, ["solo", "dos"])
    with pytest.raises(ValueError):
        build_components(ENTRY_QR_READY, ["Sara", "la clase", "15/10"])  # sin imagen
    comps = build_components(ENTRY_QR_READY, ["Sara", "la clase", "15/10"], "https://x/qr.png")
    assert comps[0]["type"] == "header" and comps[1]["type"] == "body"
