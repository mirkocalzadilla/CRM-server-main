"""Lead cerrado que reescribe → NUEVA oportunidad en Gestión IA con contexto nuevo (#163).

Un teléfono tiene N conversaciones (una por oportunidad). El webhook abre una
conversación nueva cuando la última está cerrada (`conversation.closed_at`) y reusa la
abierta en otro caso; los hooks de CRM (`move_card` manual, `card_service.sync`) marcan
`closed_at` al caer en un stage won/lost y lo limpian al volver a uno abierto. Seed
mínimo del chain sobre SQLite.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from server.modules.agent.domain.funnel_fsm import FunnelStage
from server.modules.agent.domain.models import (
    Agent,
    AgentInstance,
    Conversation,
    Product,
)
from server.modules.agent.repositories.conversation_repository import ConversationRepository
from server.modules.agent.services.webhook_service import WhatsAppWebhookService
from server.modules.crm.domain.models import Card
from server.modules.crm.repositories.board_repository import BoardRepository
from server.modules.crm.seed import seed_crm
from server.modules.crm.services.board_service import BoardService
from server.modules.crm.services.card_service import CardService

LEAD_WA_ID = "59169005037"


class _StubPublisher:
    async def publish(self, channel: str, message: dict[str, object]) -> None:
        return None


async def _seed_org(session: AsyncSession) -> tuple[uuid.UUID, AgentInstance]:
    """org + product→agent→instance + ambos pipelines (IA/Humana) seedeados."""
    org_id = uuid.uuid4()
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
    await seed_crm(session, org_id)
    await session.commit()
    return org_id, instance


async def test_webhook_reuses_open_conversation(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    async with session_factory() as session:
        org_id, instance = await _seed_org(session)
        conv = await ConversationRepository(session).add(
            Conversation(instance_id=instance.id, organization_id=org_id, external_id=LEAD_WA_ID)
        )
        await session.commit()
        open_id = conv.id
        service = WhatsAppWebhookService(session, dispatcher=None)  # type: ignore[arg-type]
        got = await service._get_or_create_conversation(instance, org_id, LEAD_WA_ID)
    assert got.id == open_id  # conversación abierta → se reusa el mismo thread


async def test_webhook_opens_new_opportunity_when_latest_closed(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    async with session_factory() as session:
        org_id, instance = await _seed_org(session)
        repo = ConversationRepository(session)
        old = await repo.add(
            Conversation(
                instance_id=instance.id,
                organization_id=org_id,
                external_id=LEAD_WA_ID,
                funnel_stage=FunnelStage.HANDED_OFF,
                is_ai_active=False,
            )
        )
        old.closed_at = datetime.now(UTC)  # oportunidad cerrada
        await session.commit()
        old_id = old.id
        service = WhatsAppWebhookService(session, dispatcher=None)  # type: ignore[arg-type]
        new = await service._get_or_create_conversation(instance, org_id, LEAD_WA_ID)
        await session.commit()
        new_id = new.id
    # Conversación nueva = oportunidad nueva, contexto limpio; la cerrada queda.
    assert new_id != old_id
    assert new.closed_at is None
    assert new.funnel_stage is FunnelStage.NEW
    assert new.is_ai_active is True
    async with session_factory() as session:
        closed = await session.get(Conversation, old_id)
        assert closed is not None and closed.closed_at is not None


async def test_card_sync_to_lost_stage_closes_conversation(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    # El agente descalifica al lead → card en "Descalificado" (lost) → conversación cerrada.
    async with session_factory() as session:
        org_id, instance = await _seed_org(session)
        conv = await ConversationRepository(session).add(
            Conversation(
                instance_id=instance.id,
                organization_id=org_id,
                external_id=LEAD_WA_ID,
                funnel_stage=FunnelStage.DISQUALIFIED,
                is_ai_active=True,
            )
        )
        await session.commit()
        conv_id = conv.id
        await CardService(session=session, publisher=_StubPublisher()).sync(conv_id, org_id)
    async with session_factory() as session:
        conv = await session.get(Conversation, conv_id)
        assert conv is not None and conv.closed_at is not None


async def test_move_card_to_won_closes_then_reopen_clears(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    async with session_factory() as session:
        org_id, instance = await _seed_org(session)
        conv = await ConversationRepository(session).add(
            Conversation(
                instance_id=instance.id,
                organization_id=org_id,
                external_id=LEAD_WA_ID,
                funnel_stage=FunnelStage.HANDED_OFF,
                is_ai_active=False,
                full_name="Lead Cerrable",  # ganar exige nombre (#241)
            )
        )
        await session.flush()
        board = BoardRepository(session)
        intake = await board.get_stage(org_id, "human", "Por atender")
        closed_stage = await board.get_stage(org_id, "human", "Cerrado")
        assert intake is not None and closed_stage is not None
        card = Card(
            organization_id=org_id,
            conversation_id=conv.id,
            stage_id=intake.id,
            title="Lead",
        )
        session.add(card)
        await session.commit()
        conv_id, card_id, intake_id, closed_id = conv.id, card.id, intake.id, closed_stage.id

    svc_kwargs = {"publisher": _StubPublisher()}
    actor = uuid.uuid4()
    # Mover a "Cerrado" (won) → cierra la conversación.
    async with session_factory() as session:
        await BoardService(session=session, **svc_kwargs).move_card(
            card_id, closed_id, actor, org_id
        )
    async with session_factory() as session:
        conv = await session.get(Conversation, conv_id)
        assert conv is not None and conv.closed_at is not None
    # Mover de vuelta a un stage abierto → reabre (limpia closed_at).
    async with session_factory() as session:
        await BoardService(session=session, **svc_kwargs).move_card(
            card_id, intake_id, actor, org_id
        )
    async with session_factory() as session:
        conv = await session.get(Conversation, conv_id)
        assert conv is not None and conv.closed_at is None
