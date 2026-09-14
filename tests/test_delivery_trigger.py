"""Disparadores de la entrega y aviso de comprobante extra (server#270, CR2).

Cubre el caso 18 de la matriz (segundo comprobante con la card ya entregada) y la
mecánica de los dos disparadores: validar el pago entrega en el acto, y un inbound del
lead reintenta una entrega que había quedado pendiente por la ventana de 24h.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from server.modules.agent.domain.funnel_fsm import FunnelStage
from server.modules.agent.domain.models import Agent, AgentInstance, Conversation, Product
from server.modules.crm.domain import card_flags, stages
from server.modules.crm.domain.models import Card, Stage
from server.modules.crm.domain.payment_models import PaymentReceipt
from server.modules.crm.repositories.board_repository import BoardRepository
from server.modules.crm.seed import seed_crm
from server.modules.crm.services.delivery_trigger import deliver_if_payment_validated
from server.modules.crm.services.extra_receipt_service import flag_extra_receipt

SessionFactory = async_sessionmaker[AsyncSession]
LEAD_WA_ID = "59170000999"


async def _seed(
    session_factory: SessionFactory, *, stage_name: str, flags: list[str] | None = None
) -> tuple[uuid.UUID, uuid.UUID, uuid.UUID]:
    """Devuelve (org_id, conversation_id, card_id)."""
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
            funnel_stage=FunnelStage.HANDED_OFF,
            is_ai_active=False,
            full_name="Lead",
        )
        session.add(conversation)
        await session.flush()
        stage = await BoardRepository(session).get_stage(org_id, stages.PIPELINE_HUMAN, stage_name)
        assert stage is not None
        card = Card(
            organization_id=org_id,
            conversation_id=conversation.id,
            stage_id=stage.id,
            title="Lead",
            flags=flags or [],
        )
        session.add(card)
        await session.commit()
        return org_id, conversation.id, card.id


async def _card_flags(session_factory: SessionFactory, card_id: uuid.UUID) -> list[str]:
    async with session_factory() as session:
        card = await session.get(Card, card_id)
        assert card is not None
        return card_flags.normalize(card.flags)


# --------------------------- caso 18: comprobante extra ---------------------------


async def test_receipt_with_card_already_delivered_is_flagged(
    session_factory: SessionFactory,
) -> None:
    org_id, conversation_id, card_id = await _seed(session_factory, stage_name=stages.DELIVERED)
    async with session_factory() as session:
        assert await flag_extra_receipt(session, conversation_id, org_id) is True
    assert card_flags.EXTRA_RECEIPT in await _card_flags(session_factory, card_id)


async def test_receipt_with_card_already_paid_is_flagged(
    session_factory: SessionFactory,
) -> None:
    org_id, conversation_id, card_id = await _seed(
        session_factory, stage_name=stages.PAYMENT_VALIDATED
    )
    async with session_factory() as session:
        assert await flag_extra_receipt(session, conversation_id, org_id) is True
    assert card_flags.EXTRA_RECEIPT in await _card_flags(session_factory, card_id)


async def test_receipt_while_awaiting_validation_is_not_flagged(
    session_factory: SessionFactory,
) -> None:
    """El comprobante que se está esperando no es "extra": es el que corresponde."""
    org_id, conversation_id, card_id = await _seed(
        session_factory, stage_name=stages.PAYMENT_VALIDATION
    )
    async with session_factory() as session:
        assert await flag_extra_receipt(session, conversation_id, org_id) is False
    assert await _card_flags(session_factory, card_id) == []


async def test_extra_receipt_is_flagged_once_not_per_photo(
    session_factory: SessionFactory,
) -> None:
    """Tres fotos seguidas no llenan la card de avisos repetidos, pero las tres siguen
    siendo "extra" (el valor de retorno responde eso, no "¿lo marqué recién?")."""
    org_id, conversation_id, card_id = await _seed(session_factory, stage_name=stages.DELIVERED)
    async with session_factory() as session:
        assert await flag_extra_receipt(session, conversation_id, org_id) is True
        assert await flag_extra_receipt(session, conversation_id, org_id) is True
        assert await flag_extra_receipt(session, conversation_id, org_id) is True
    assert (await _card_flags(session_factory, card_id)).count(card_flags.EXTRA_RECEIPT) == 1


async def test_extra_receipt_does_not_move_the_card(session_factory: SessionFactory) -> None:
    org_id, conversation_id, card_id = await _seed(session_factory, stage_name=stages.DELIVERED)
    async with session_factory() as session:
        await flag_extra_receipt(session, conversation_id, org_id)
    async with session_factory() as session:
        card = await session.get(Card, card_id)
        assert card is not None
        stage = await session.get(Stage, card.stage_id)
        assert stage is not None and stage.name == stages.DELIVERED


# --------------------------- disparador del move ---------------------------


async def test_moving_to_another_stage_does_not_trigger_delivery(
    session_factory: SessionFactory,
) -> None:
    """Solo "Pago validado" dispara la entrega; los demás moves no hacen nada."""
    org_id, _conversation_id, card_id = await _seed(
        session_factory, stage_name=stages.PAYMENT_VALIDATION
    )
    async with session_factory() as session:
        stage = await BoardRepository(session).get_stage(
            org_id, stages.PIPELINE_HUMAN, stages.PAYMENT_VALIDATION
        )
        assert stage is not None
        outcome = await deliver_if_payment_validated(session, stage.id, card_id, org_id)
    assert outcome is None


async def test_moving_to_payment_validated_triggers_delivery(
    session_factory: SessionFactory,
) -> None:
    """El disparador corre aunque la card no tenga servicio: devuelve el resultado
    (bloqueado, en este caso) en vez de None, que es lo que distingue "no se intentó"
    de "se intentó y no se pudo"."""
    org_id, _conversation_id, card_id = await _seed(
        session_factory, stage_name=stages.PAYMENT_VALIDATED
    )
    async with session_factory() as session:
        stage = await BoardRepository(session).get_stage(
            org_id, stages.PIPELINE_HUMAN, stages.PAYMENT_VALIDATED
        )
        assert stage is not None
        outcome = await deliver_if_payment_validated(session, stage.id, card_id, org_id)
    assert outcome is not None
    assert outcome.delivered is False  # sin servicio cargado no hay qué entregar
    assert card_flags.NO_MODALITY in await _card_flags(session_factory, card_id)


async def _add_receipt(
    session_factory: SessionFactory,
    org_id: uuid.UUID,
    card_id: uuid.UUID,
    *,
    approved_by: str | None = None,
) -> uuid.UUID:
    async with session_factory() as session:
        receipt = PaymentReceipt(
            organization_id=org_id,
            card_id=card_id,
            wamid=f"wamid.{uuid.uuid4()}",
            image_sha256=str(uuid.uuid4()),
            verdict="fail",
            checks=[{"code": "amount", "passed": False, "detail": "monto distinto"}],
            extracted={"amount": "900.00"},
            approved_at=datetime.now(UTC) if approved_by else None,
            approved_by=approved_by,
        )
        session.add(receipt)
        await session.commit()
        return receipt.id


async def test_moving_to_payment_validated_records_the_operators_approval(
    session_factory: SessionFactory,
) -> None:
    """Arrastrar la card a "Pago validado" es validar el pago: el comprobante lo registra
    y el aviso "revisá este comprobante" deja de aplicar (server#292). Sin esto, el panel
    seguía ofreciendo validar un pago que ya se había entregado por arrastre."""
    org_id, _conversation_id, card_id = await _seed(
        session_factory, stage_name=stages.PAYMENT_VALIDATED, flags=[card_flags.RECEIPT_REVIEW]
    )
    receipt_id = await _add_receipt(session_factory, org_id, card_id)
    operator = uuid.uuid4()
    async with session_factory() as session:
        stage = await BoardRepository(session).get_stage(
            org_id, stages.PIPELINE_HUMAN, stages.PAYMENT_VALIDATED
        )
        assert stage is not None
        await deliver_if_payment_validated(session, stage.id, card_id, org_id, approved_by=operator)
    async with session_factory() as session:
        receipt = await session.get(PaymentReceipt, receipt_id)
        assert receipt is not None
        assert receipt.approved_by == str(operator)
        assert receipt.approved_at is not None
    assert card_flags.RECEIPT_REVIEW not in await _card_flags(session_factory, card_id)


async def test_moving_to_payment_validated_keeps_a_previous_approval(
    session_factory: SessionFactory,
) -> None:
    """La primera aprobación es la que cuenta: un arrastre posterior no pisa la del
    sistema."""
    org_id, _conversation_id, card_id = await _seed(
        session_factory, stage_name=stages.PAYMENT_VALIDATED
    )
    receipt_id = await _add_receipt(session_factory, org_id, card_id, approved_by="system")
    async with session_factory() as session:
        stage = await BoardRepository(session).get_stage(
            org_id, stages.PIPELINE_HUMAN, stages.PAYMENT_VALIDATED
        )
        assert stage is not None
        await deliver_if_payment_validated(
            session, stage.id, card_id, org_id, approved_by=uuid.uuid4()
        )
    async with session_factory() as session:
        receipt = await session.get(PaymentReceipt, receipt_id)
        assert receipt is not None
        assert receipt.approved_by == "system"
