"""El sync no hace retroceder una card dentro del pipeline humano (server#270).

Bug que arregla: con la conversación abierta y el último handoff `payment_validation`,
**cualquier** inbound del lead ("gracias", un sticker) re-sincronizaba la card a "Por
validar pago" y deshacía el trabajo del operador — una oportunidad ya entregada volvía
a la bandeja de pendientes.

La regla es asimétrica a propósito: dentro del pipeline humano el stage solo avanza,
pero cruzar de pipeline sí se permite (el handoff, y reactivar la IA con el toggle, no
son retrocesos sino cambios de dueño de la conversación).

Estos tests cubren las ramas de `sync` sobre una card **preexistente**, que antes no
tenían ninguna cobertura.
"""

from __future__ import annotations

import uuid

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from server.modules.agent.domain.funnel_fsm import FunnelStage
from server.modules.agent.domain.models import (
    Agent,
    AgentInstance,
    Conversation,
    HandoffEvent,
    Product,
)
from server.modules.crm.domain import stages
from server.modules.crm.domain.models import Card, Stage
from server.modules.crm.seed import seed_crm
from server.modules.crm.services.card_service import CardService

SessionFactory = async_sessionmaker[AsyncSession]
LEAD_WA_ID = "59170000001"


class _NoopPublisher:
    async def publish(self, channel: str, message: object) -> None:
        return None


async def _seed(
    session_factory: SessionFactory,
    *,
    stage_name: str,
    pipeline_kind: str = stages.PIPELINE_HUMAN,
    funnel_stage: FunnelStage = FunnelStage.HANDED_OFF,
    is_ai_active: bool = False,
    handoff_reason: str | None = "payment_validation",
) -> tuple[uuid.UUID, uuid.UUID, uuid.UUID]:
    """Devuelve (org_id, conversation_id, card_id) con la card en `stage_name`."""
    org_id = uuid.uuid4()
    async with session_factory() as session:
        await seed_crm(session, org_id)
        if await session.get(Product, "cursos-mirko") is None:
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
        instance = AgentInstance(agent_id=agent.id, display_name="WA")
        session.add(instance)
        await session.flush()
        conversation = Conversation(
            instance_id=instance.id,
            organization_id=org_id,
            external_id=LEAD_WA_ID,
            funnel_stage=funnel_stage,
            is_ai_active=is_ai_active,
            full_name="Lead Test",
        )
        session.add(conversation)
        await session.flush()
        if handoff_reason is not None:
            session.add(
                HandoffEvent(
                    agent_id=agent.id,
                    organization_id=org_id,
                    thread_id=str(conversation.id),
                    reason=handoff_reason,
                )
            )
        stage = await _stage(session, org_id, pipeline_kind, stage_name)
        card = Card(
            organization_id=org_id,
            conversation_id=conversation.id,
            stage_id=stage.id,
            title="Lead Test",
        )
        session.add(card)
        await session.commit()
        return org_id, conversation.id, card.id


async def _stage(session: AsyncSession, org_id: uuid.UUID, kind: str, name: str) -> Stage:
    from server.modules.crm.repositories.board_repository import BoardRepository

    stage = await BoardRepository(session).get_stage(org_id, kind, name)
    assert stage is not None, f"stage {name} no seedeado"
    return stage


async def _sync_and_read_stage(
    session_factory: SessionFactory,
    org_id: uuid.UUID,
    conversation_id: uuid.UUID,
    card_id: uuid.UUID,
) -> str:
    async with session_factory() as session:
        await CardService(session=session, publisher=_NoopPublisher()).sync(conversation_id, org_id)
    async with session_factory() as session:
        card = await session.get(Card, card_id)
        assert card is not None
        stage = await session.get(Stage, card.stage_id)
        assert stage is not None
        return stage.name


async def test_thanks_after_delivery_does_not_send_card_back(
    session_factory: SessionFactory,
) -> None:
    """El caso del bug: card entregada + inbound del lead ⇒ NO vuelve a validar pago."""
    org_id, conversation_id, card_id = await _seed(session_factory, stage_name=stages.DELIVERED)
    result = await _sync_and_read_stage(session_factory, org_id, conversation_id, card_id)
    assert result == stages.DELIVERED


async def test_thanks_after_payment_validated_does_not_send_card_back(
    session_factory: SessionFactory,
) -> None:
    org_id, conversation_id, card_id = await _seed(
        session_factory, stage_name=stages.PAYMENT_VALIDATED
    )
    result = await _sync_and_read_stage(session_factory, org_id, conversation_id, card_id)
    assert result == stages.PAYMENT_VALIDATED


async def test_no_regression_leaves_no_move_row(session_factory: SessionFactory) -> None:
    """No basta con que el stage no cambie: tampoco debe quedar un move fantasma en el
    historial que el operador no hizo."""
    from server.modules.crm.repositories.card_repository import CardRepository

    org_id, conversation_id, card_id = await _seed(session_factory, stage_name=stages.DELIVERED)
    await _sync_and_read_stage(session_factory, org_id, conversation_id, card_id)
    async with session_factory() as session:
        moves = await CardRepository(session).list_moves(card_id)
    assert moves == []


async def test_sync_still_advances_within_human_pipeline(
    session_factory: SessionFactory,
) -> None:
    """El guard no congela la card: desde el intake genérico, un comprobante sí la
    hace avanzar a "Por validar pago"."""
    org_id, conversation_id, card_id = await _seed(session_factory, stage_name=stages.HUMAN_INTAKE)
    result = await _sync_and_read_stage(session_factory, org_id, conversation_id, card_id)
    assert result == stages.PAYMENT_VALIDATION


async def test_reactivating_ai_moves_card_back_to_ia_pipeline(
    session_factory: SessionFactory,
) -> None:
    """Cruzar de pipeline no es un retroceso: si el operador reactiva la IA, la card
    tiene que volver al tablero del bot aunque estuviera más adelante en el humano."""
    org_id, conversation_id, card_id = await _seed(
        session_factory,
        stage_name=stages.DELIVERED,
        funnel_stage=FunnelStage.ENGAGING,
        is_ai_active=True,
        handoff_reason=None,
    )
    result = await _sync_and_read_stage(session_factory, org_id, conversation_id, card_id)
    assert result == stages.IA_ENGAGING


async def test_handoff_still_crosses_from_ia_to_human(
    session_factory: SessionFactory,
) -> None:
    """El cruce de siempre: card en el pipeline IA + handoff de pago ⇒ pipeline humano."""
    org_id, conversation_id, card_id = await _seed(
        session_factory,
        stage_name=stages.IA_QUALIFIED,
        pipeline_kind=stages.PIPELINE_IA,
    )
    result = await _sync_and_read_stage(session_factory, org_id, conversation_id, card_id)
    assert result == stages.PAYMENT_VALIDATION


async def test_ia_pipeline_is_not_ratcheted(session_factory: SessionFactory) -> None:
    """El guard es solo del pipeline humano: dentro del de IA el funnel manda, y puede
    retroceder legítimamente (p. ej. un lead que se reactiva desde una etapa avanzada)."""
    org_id, conversation_id, card_id = await _seed(
        session_factory,
        stage_name=stages.IA_QUALIFIED,
        pipeline_kind=stages.PIPELINE_IA,
        funnel_stage=FunnelStage.ENGAGING,
        is_ai_active=True,
        handoff_reason=None,
    )
    result = await _sync_and_read_stage(session_factory, org_id, conversation_id, card_id)
    assert result == stages.IA_ENGAGING
