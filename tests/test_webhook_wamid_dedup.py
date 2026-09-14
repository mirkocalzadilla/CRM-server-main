"""Dedup por wamid en la ingesta del webhook (caso 16 de la matriz, server#272).

Meta reintenta la entrega del webhook. Sin dedup, un reintento duplica el turno del
lead: el agente responde dos veces, y un comprobante se valida dos veces. Y si la
oportunidad anterior estaba cerrada, el reintento además **crea una card fantasma** —
por eso el chequeo va antes de resolver la conversación, no después.
"""

from __future__ import annotations

import uuid

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from server.modules.agent.domain.models import (
    Agent,
    AgentInstance,
    AiChatHistory,
    Conversation,
    Product,
)
from server.modules.agent.domain.whatsapp_schemas import WAWebhookPayload
from server.modules.agent.services.webhook_service import WhatsAppWebhookService

SessionFactory = async_sessionmaker[AsyncSession]
INSTANCE_NUMBER = "+59100000000"
LEAD_WA_ID = "59170000555"
WAMID = "wamid.DEDUP1"


class _StubDispatcher:
    def __init__(self) -> None:
        self.turns: list[uuid.UUID] = []
        self.vision: list[str] = []

    async def enqueue(self, conversation_id: uuid.UUID, tenant_id: uuid.UUID) -> None:
        self.turns.append(conversation_id)

    async def enqueue_vision(
        self, conversation_id: uuid.UUID, tenant_id: uuid.UUID, wamid: str
    ) -> None:
        self.vision.append(wamid)


def _payload(wamid: str, text: str = "hola") -> WAWebhookPayload:
    return WAWebhookPayload.model_validate(
        {
            "object": "whatsapp_business_account",
            "entry": [
                {
                    "id": "WABA",
                    "changes": [
                        {
                            "field": "messages",
                            "value": {
                                "messaging_product": "whatsapp",
                                "metadata": {
                                    "display_phone_number": INSTANCE_NUMBER,
                                    "phone_number_id": "PNID",
                                },
                                "contacts": [{"profile": {"name": "Lead"}, "wa_id": LEAD_WA_ID}],
                                "messages": [
                                    {
                                        "from": LEAD_WA_ID,
                                        "id": wamid,
                                        "timestamp": "1700000000",
                                        "type": "text",
                                        "text": {"body": text},
                                    }
                                ],
                            },
                        }
                    ],
                }
            ],
        }
    )


async def _seed_instance(session_factory: SessionFactory) -> uuid.UUID:
    org_id = uuid.uuid4()
    async with session_factory() as session:
        session.add(Product(slug="cursos-mirko", display_name="Cursos"))
        await session.flush()
        agent = Agent(
            organization_id=org_id,
            product_slug="cursos-mirko",
            display_name="Agente",
            system_prompt="x",
            model="claude-haiku-4-5-20251001",
        )
        session.add(agent)
        await session.flush()
        session.add(
            AgentInstance(agent_id=agent.id, display_name="WA", whatsapp_number=INSTANCE_NUMBER)
        )
        await session.commit()
    return org_id


async def _turn_count(session_factory: SessionFactory) -> int:
    async with session_factory() as session:
        return int(
            (await session.execute(select(func.count()).select_from(AiChatHistory))).scalar_one()
        )


async def _conversation_count(session_factory: SessionFactory) -> int:
    async with session_factory() as session:
        return int(
            (await session.execute(select(func.count()).select_from(Conversation))).scalar_one()
        )


async def test_meta_retry_with_same_wamid_stores_one_turn(
    session_factory: SessionFactory,
) -> None:
    """Un solo turno y un solo dispatch, aunque Meta entregue el mismo mensaje dos veces."""
    await _seed_instance(session_factory)
    dispatcher = _StubDispatcher()

    for _ in range(3):
        async with session_factory() as session:
            service = WhatsAppWebhookService(session, dispatcher)  # type: ignore[arg-type]
            await service.process(_payload(WAMID))
            await session.commit()

    assert await _turn_count(session_factory) == 1
    assert len(dispatcher.turns) == 1
    assert await _conversation_count(session_factory) == 1


async def test_different_wamids_are_both_stored(session_factory: SessionFactory) -> None:
    """El dedup no puede comerse un mensaje distinto del mismo lead."""
    await _seed_instance(session_factory)
    dispatcher = _StubDispatcher()

    for wamid in ("wamid.A", "wamid.B"):
        async with session_factory() as session:
            service = WhatsAppWebhookService(session, dispatcher)  # type: ignore[arg-type]
            await service.process(_payload(wamid, text=f"mensaje {wamid}"))
            await session.commit()

    assert await _turn_count(session_factory) == 2
    assert len(dispatcher.turns) == 2


async def test_retry_does_not_create_a_phantom_conversation(
    session_factory: SessionFactory,
) -> None:
    """El caso que hace que el orden importe: si la oportunidad anterior quedó cerrada,
    un reintento que se dedupe *después* de resolver la conversación ya creó una nueva —
    o sea, una card fantasma en el tablero."""
    await _seed_instance(session_factory)
    dispatcher = _StubDispatcher()
    async with session_factory() as session:
        service = WhatsAppWebhookService(session, dispatcher)  # type: ignore[arg-type]
        await service.process(_payload(WAMID))
        await session.commit()

    # La oportunidad se cierra (won/lost), como haría el hook de cierre.
    async with session_factory() as session:
        from datetime import UTC, datetime

        conversation = (await session.execute(select(Conversation))).scalars().one()
        conversation.closed_at = datetime.now(UTC)
        await session.commit()

    async with session_factory() as session:
        service = WhatsAppWebhookService(session, dispatcher)  # type: ignore[arg-type]
        await service.process(_payload(WAMID))  # reintento de Meta
        await session.commit()

    assert await _conversation_count(session_factory) == 1  # ninguna conversación nueva
    assert await _turn_count(session_factory) == 1
    assert len(dispatcher.turns) == 1


async def test_unique_index_is_the_hard_guarantee(session_factory: SessionFactory) -> None:
    """El chequeo previo evita el trabajo; el índice es la red para dos entregas
    concurrentes que pasan el SELECT las dos."""
    import pytest
    from sqlalchemy.exc import IntegrityError

    org_id = await _seed_instance(session_factory)
    async with session_factory() as session:
        agent = (await session.execute(select(Agent))).scalars().one()
        for _ in range(2):
            session.add(
                AiChatHistory(
                    agent_id=agent.id,
                    organization_id=org_id,
                    thread_id="t",
                    session_id=LEAD_WA_ID,
                    message={"role": "user", "content": "x", "wamid": WAMID},
                )
            )
        with pytest.raises(IntegrityError):
            await session.commit()


async def test_assistant_turns_without_wamid_do_not_collide(
    session_factory: SessionFactory,
) -> None:
    """El índice es parcial: las respuestas del agente no llevan wamid y son muchas."""
    org_id = await _seed_instance(session_factory)
    async with session_factory() as session:
        agent = (await session.execute(select(Agent))).scalars().one()
        for index in range(3):
            session.add(
                AiChatHistory(
                    agent_id=agent.id,
                    organization_id=org_id,
                    thread_id="t",
                    session_id=LEAD_WA_ID,
                    message={"role": "assistant", "content": f"respuesta {index}"},
                )
            )
        await session.commit()
    assert await _turn_count(session_factory) == 3
