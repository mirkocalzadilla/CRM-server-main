"""Outbound (etapa A): el webhook actualiza estados de plantillas y atiende la baja (BAJA)."""

from __future__ import annotations

import uuid

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from server.modules.agent.domain.models import Agent, AgentInstance, AiChatHistory, Product
from server.modules.agent.domain.whatsapp_schemas import (
    WAChange,
    WAEntry,
    WAIncomingMessage,
    WAMetadata,
    WAValue,
    WAWebhookPayload,
)
from server.modules.agent.services.webhook_service import WhatsAppWebhookService
from server.modules.outbound.domain.models import (
    STATUS_DELIVERED,
    STATUS_FAILED,
    STATUS_SENT,
    MarketingOptOut,
    OutboundMessage,
)
from server.modules.outbound.domain.opt_out import OPT_OUT_REPLY, is_opt_out_request
from server.modules.outbound.services.status_service import OutboundStatusService

NUMBER = "+59177712345"
LEAD = "59170000009"


class _StubDispatcher:
    def __init__(self) -> None:
        self.enqueued: list[uuid.UUID] = []

    async def enqueue(self, conversation_id: uuid.UUID, tenant_id: uuid.UUID) -> None:
        self.enqueued.append(conversation_id)


class _StubSender:
    def __init__(self) -> None:
        self.texts: list[tuple[str, str]] = []

    async def send_text(self, to: str, body: str) -> None:
        self.texts.append((to, body))

    async def send_image(self, to: str, link: str, caption: str = "") -> None: ...

    async def send_document(self, to: str, link: str, filename: str, caption: str = "") -> None: ...


async def _seed(session_factory: async_sessionmaker[AsyncSession]) -> uuid.UUID:
    org_id = uuid.uuid4()
    async with session_factory() as session:
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
        session.add(AgentInstance(agent_id=agent.id, display_name="WA", whatsapp_number=NUMBER))
        await session.commit()
    return org_id


def _text_payload(text: str) -> WAWebhookPayload:
    return WAWebhookPayload(
        object="whatsapp_business_account",
        entry=[
            WAEntry(
                id="waba-1",
                changes=[
                    WAChange(
                        field="messages",
                        value=WAValue(
                            messaging_product="whatsapp",
                            metadata=WAMetadata(display_phone_number=NUMBER, phone_number_id="1"),
                            messages=[
                                WAIncomingMessage.model_validate(
                                    {
                                        "id": f"wamid.in.{uuid.uuid4().hex[:6]}",
                                        "from": LEAD,
                                        "timestamp": "1790000000",
                                        "type": "text",
                                        "text": {"body": text},
                                    }
                                )
                            ],
                        ),
                    )
                ],
            )
        ],
    )


def test_opt_out_keywords_are_recognized_loosely() -> None:
    assert is_opt_out_request("BAJA")
    assert is_opt_out_request("  baja. ")
    assert is_opt_out_request("No quiero recibir más mensajes")
    assert not is_opt_out_request("baja el precio?")
    assert not is_opt_out_request("hola, quiero info del curso")


async def test_baja_registers_opt_out_and_skips_agent(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    org_id = await _seed(session_factory)
    dispatcher, sender = _StubDispatcher(), _StubSender()
    async with session_factory() as session:
        await WhatsAppWebhookService(session, dispatcher, sender).process(_text_payload("Baja"))  # type: ignore[arg-type]
        await session.commit()

    assert dispatcher.enqueued == []
    assert sender.texts == [(LEAD, OPT_OUT_REPLY)]
    async with session_factory() as session:
        opt_outs = (await session.execute(select(MarketingOptOut))).scalars().all()
        history = (await session.execute(select(AiChatHistory))).scalars().all()
    assert [(o.organization_id, o.wa_id) for o in opt_outs] == [(org_id, LEAD)]
    roles = [h.message["role"] for h in history]
    assert roles == ["user", "assistant"]  # the lead's BAJA + the confirmation


async def test_regular_text_still_reaches_the_agent(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    await _seed(session_factory)
    dispatcher = _StubDispatcher()
    async with session_factory() as session:
        await WhatsAppWebhookService(session, dispatcher, _StubSender()).process(  # type: ignore[arg-type]
            _text_payload("hola, quiero info")
        )
        await session.commit()
    assert len(dispatcher.enqueued) == 1


async def test_statuses_update_outbound_rows_with_precedence(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    org_id = uuid.uuid4()
    async with session_factory() as session:
        session.add_all(
            [
                OutboundMessage(
                    organization_id=org_id,
                    wa_id=LEAD,
                    template_name="recordatorio_evento",
                    purpose="event_reminder",
                    status=STATUS_SENT,
                    wamid="wamid.A",
                ),
                OutboundMessage(
                    organization_id=org_id,
                    wa_id=LEAD,
                    template_name="reactivacion_leads",
                    purpose="reactivation",
                    status=STATUS_SENT,
                    wamid="wamid.B",
                ),
            ]
        )
        await session.commit()

    statuses: list[dict[str, object]] = [
        {"id": "wamid.A", "status": "delivered", "timestamp": "1790000000"},
        {"id": "wamid.A", "status": "sent", "timestamp": "1789999999"},  # tardío: no baja
        {
            "id": "wamid.B",
            "status": "failed",
            "timestamp": "1790000001",
            "errors": [{"code": 131049, "title": "Meta chose not to deliver"}],
        },
        {"id": "wamid.unknown", "status": "read"},  # no es una plantilla nuestra
    ]
    async with session_factory() as session:
        updated = await OutboundStatusService(session).apply(statuses)
        await session.commit()
    assert updated == 2

    async with session_factory() as session:
        rows = {r.wamid: r for r in (await session.execute(select(OutboundMessage))).scalars()}
    assert rows["wamid.A"].status == STATUS_DELIVERED
    assert rows["wamid.B"].status == STATUS_FAILED
    assert rows["wamid.B"].error_code == 131049
    assert "not to deliver" in (rows["wamid.B"].error_detail or "")
