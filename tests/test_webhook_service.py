"""Ingest de WhatsApp (M1/C): resolución del número + persistencia del turno + dispatch.

Cubre la robustez del matcheo de número (FIX auditoría): el ``display_phone_number``
de Meta puede venir sin ``+`` o con espacios y no byte-matchear el número guardado;
sin normalización, todo mensaje entrante se descartaría como ``unknown_instance``.
Seed mínimo (product→agent→instance) sobre SQLite, dispatcher stub (sin Redis).
"""

from __future__ import annotations

import uuid

import pytest
import sentry_sdk
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker
from structlog.testing import capture_logs

from server.modules.agent.domain.models import (
    Agent,
    AgentInstance,
    AiChatHistory,
    Product,
)
from server.modules.agent.domain.whatsapp_schemas import (
    WAChange,
    WAEntry,
    WAIncomingMessage,
    WAMetadata,
    WAText,
    WAValue,
    WAWebhookPayload,
)
from server.modules.agent.repositories.agent_instance_repository import (
    AgentInstanceRepository,
    _digits,
)
from server.modules.agent.services.webhook_service import WhatsAppWebhookService

STORED_NUMBER = "+59177712345"  # como queda en el seed (con '+')
LEAD_WA_ID = "59170000000"


class _StubDispatcher:
    def __init__(self) -> None:
        self.enqueued: list[tuple[uuid.UUID, uuid.UUID]] = []

    async def enqueue(self, conversation_id: uuid.UUID, tenant_id: uuid.UUID) -> None:
        self.enqueued.append((conversation_id, tenant_id))


async def _seed_instance(
    session_factory: async_sessionmaker[AsyncSession], *, number: str
) -> uuid.UUID:
    """Crea product→agent→instance con `number` y devuelve la org_id del agente."""
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
        session.add(AgentInstance(agent_id=agent.id, display_name="WA", whatsapp_number=number))
        await session.commit()
    return org_id


# ---------- repo: matcheo normalizado ----------


def test_digits_strips_formatting() -> None:
    assert _digits("+591 777-12345") == "59177712345"
    assert _digits("59177712345") == "59177712345"
    assert _digits("") == ""


async def test_exact_match_resolves_instance(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    await _seed_instance(session_factory, number=STORED_NUMBER)
    async with session_factory() as session:
        found = await AgentInstanceRepository(session).get_by_whatsapp_number(STORED_NUMBER)
    assert found is not None


async def test_meta_number_without_plus_still_resolves(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    # Meta manda los dígitos sin '+'; el fallback normalizado debe encontrar la instancia.
    await _seed_instance(session_factory, number=STORED_NUMBER)
    async with session_factory() as session:
        found = await AgentInstanceRepository(session).get_by_whatsapp_number("59177712345")
    assert found is not None


async def test_unknown_number_returns_none(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    await _seed_instance(session_factory, number=STORED_NUMBER)
    async with session_factory() as session:
        found = await AgentInstanceRepository(session).get_by_whatsapp_number("19998887777")
    assert found is None


# ---------- ingest end-to-end (FIX 2 por el camino real) ----------


def _payload(display_number: str, *, text: str) -> WAWebhookPayload:
    return WAWebhookPayload(
        object="whatsapp_business_account",
        entry=[
            WAEntry(
                id="entry1",
                changes=[
                    WAChange(
                        field="messages",
                        value=WAValue(
                            messaging_product="whatsapp",
                            metadata=WAMetadata(
                                display_phone_number=display_number, phone_number_id="pnid"
                            ),
                            messages=[
                                WAIncomingMessage.model_validate(
                                    {
                                        "from": LEAD_WA_ID,
                                        "id": "wamid.1",
                                        "timestamp": "1700000000",
                                        "type": "text",
                                        "text": WAText(body=text),
                                    }
                                )
                            ],
                        ),
                    )
                ],
            )
        ],
    )


# Nota: la persistencia positiva del turno (number normalizado → turno guardado →
# dispatch) se valida en el e2e contra Postgres: `ai_chat_histories.message_order`
# es BIGSERIAL (secuencia de BD) y SQLite no lo autogenera. Acá cubrimos el ruteo
# del número (repo, arriba) y el descarte del número desconocido por el camino real.


async def test_ingest_drops_truly_unknown_number(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    await _seed_instance(session_factory, number=STORED_NUMBER)
    dispatcher = _StubDispatcher()

    async with session_factory() as session:
        service = WhatsAppWebhookService(session, dispatcher)  # type: ignore[arg-type]
        await service.process(_payload("10000000000", text="hola"))
        await session.commit()

    async with session_factory() as session:
        history_count = (
            await session.execute(select(func.count()).select_from(AiChatHistory))
        ).scalar_one()
    assert history_count == 0
    assert dispatcher.enqueued == []


# ---------- statuses: la muerte asincrona de un mensaje aceptado (#298) ----------


def _status_payload(*statuses: dict[str, object]) -> WAWebhookPayload:
    """Webhook de statuses: mismo `field: messages`, sin `messages` adentro."""
    return WAWebhookPayload(
        object="whatsapp_business_account",
        entry=[
            WAEntry(
                id="entry1",
                changes=[
                    WAChange(
                        field="messages",
                        value=WAValue(
                            messaging_product="whatsapp",
                            metadata=WAMetadata(
                                display_phone_number=STORED_NUMBER, phone_number_id="pnid"
                            ),
                            statuses=list(statuses),
                        ),
                    )
                ],
            )
        ],
    )


async def test_failed_status_is_logged_and_reported(
    session_factory: async_sessionmaker[AsyncSession],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    await _seed_instance(session_factory, number=STORED_NUMBER)
    captured_messages: list[str] = []
    monkeypatch.setattr(
        sentry_sdk, "capture_message", lambda msg, level=None: captured_messages.append(msg)
    )
    errors = [{"code": 131053, "title": "Media upload error"}]

    async with session_factory() as session:
        service = WhatsAppWebhookService(session, _StubDispatcher())  # type: ignore[arg-type]
        with capture_logs() as logs:
            await service.process(
                _status_payload(
                    {"id": "wamid.out1", "status": "delivered", "recipient_id": "59170000000"},
                    {
                        "id": "wamid.out2",
                        "status": "failed",
                        "recipient_id": "59170000000",
                        "errors": errors,
                    },
                )
            )

    failed = [log for log in logs if log["event"] == "whatsapp.delivery_failed"]
    assert len(failed) == 1
    assert failed[0]["wamid"] == "wamid.out2"
    assert failed[0]["errors"] == errors
    assert captured_messages == ["whatsapp.delivery_failed"]


async def test_non_failed_statuses_stay_silent(
    session_factory: async_sessionmaker[AsyncSession],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    await _seed_instance(session_factory, number=STORED_NUMBER)
    captured_messages: list[str] = []
    monkeypatch.setattr(
        sentry_sdk, "capture_message", lambda msg, level=None: captured_messages.append(msg)
    )

    async with session_factory() as session:
        service = WhatsAppWebhookService(session, _StubDispatcher())  # type: ignore[arg-type]
        with capture_logs() as logs:
            await service.process(
                _status_payload(
                    {"id": "wamid.out1", "status": "sent", "recipient_id": "59170000000"},
                    {"id": "wamid.out1", "status": "read", "recipient_id": "59170000000"},
                )
            )

    assert all(log["event"] != "whatsapp.delivery_failed" for log in logs)
    assert captured_messages == []
