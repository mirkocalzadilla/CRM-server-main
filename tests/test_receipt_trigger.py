"""Second trigger of the receipt validation (server#290).

The race this closes: the webhook enqueued the vision job together with the agent turn,
the job needed the card in "Por validar pago", and the card got there only after the
turn's handoff — so the job saw Gestión IA and dropped the receipt. After the turn, the
dispatch handler re-enqueues the photos of that turn if the card now waits for them.
"""

from __future__ import annotations

import uuid

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from server.modules.agent.domain.funnel_fsm import FunnelStage
from server.modules.agent.domain.models import (
    Agent,
    AgentInstance,
    AiChatHistory,
    Conversation,
    Product,
)
from server.modules.crm.domain import stages
from server.modules.crm.domain.models import Card
from server.modules.crm.domain.payment_models import PaymentReceipt
from server.modules.crm.repositories.board_repository import BoardRepository
from server.modules.crm.seed import seed_crm
from server.modules.crm.services.receipt_trigger import enqueue_receipts_awaiting_validation

SessionFactory = async_sessionmaker[AsyncSession]
LEAD_WA_ID = "59170000777"


class _StubQueue:
    def __init__(self) -> None:
        self.enqueued: list[str] = []

    async def enqueue_vision(
        self, conversation_id: uuid.UUID, tenant_id: uuid.UUID, wamid: str
    ) -> None:
        self.enqueued.append(wamid)


async def _seed(
    session_factory: SessionFactory,
    *,
    pipeline: str = stages.PIPELINE_HUMAN,
    stage_name: str = stages.PAYMENT_VALIDATION,
) -> tuple[uuid.UUID, uuid.UUID, uuid.UUID, uuid.UUID]:
    """Devuelve (org_id, agent_id, conversation_id, card_id)."""
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
        )
        session.add(conversation)
        await session.flush()
        stage = await BoardRepository(session).get_stage(org_id, pipeline, stage_name)
        assert stage is not None
        card = Card(
            organization_id=org_id,
            conversation_id=conversation.id,
            stage_id=stage.id,
            title="Lead",
        )
        session.add(card)
        await session.commit()
        return org_id, agent.id, conversation.id, card.id


async def _add_turn(
    session_factory: SessionFactory,
    org_id: uuid.UUID,
    agent_id: uuid.UUID,
    conversation_id: uuid.UUID,
    message: dict[str, object],
) -> int:
    """Inserta un turno y devuelve su `message_order`."""
    async with session_factory() as session:
        row = AiChatHistory(
            agent_id=agent_id,
            organization_id=org_id,
            thread_id=str(conversation_id),
            session_id=LEAD_WA_ID,
            message=message,
        )
        session.add(row)
        await session.commit()
        await session.refresh(row)
        return int(row.message_order)


def _photo(wamid: str) -> dict[str, object]:
    return {
        "role": "user",
        "content": "[image: sin descripción]",
        "wamid": wamid,
        "media_type": "image",
        "media_path": f"org/{wamid}.jpg",
    }


async def _trigger(
    session_factory: SessionFactory,
    org_id: uuid.UUID,
    conversation_id: uuid.UUID,
    *,
    after_order: int,
) -> tuple[list[str], list[str]]:
    queue = _StubQueue()
    async with session_factory() as session:
        result = await enqueue_receipts_awaiting_validation(
            session, queue, conversation_id, org_id, after_order=after_order
        )
    return result, queue.enqueued


async def test_photo_of_the_handoff_turn_is_enqueued_once_the_card_waits(
    session_factory: SessionFactory,
) -> None:
    org_id, agent_id, conversation_id, _card = await _seed(session_factory)
    before = await _add_turn(
        session_factory, org_id, agent_id, conversation_id, {"role": "user", "content": "hola"}
    )
    await _add_turn(session_factory, org_id, agent_id, conversation_id, _photo("wamid.photo"))
    await _add_turn(
        session_factory,
        org_id,
        agent_id,
        conversation_id,
        {"role": "assistant", "content": "Recibí tu comprobante!"},
    )

    result, enqueued = await _trigger(session_factory, org_id, conversation_id, after_order=before)

    assert result == ["wamid.photo"]
    assert enqueued == ["wamid.photo"]


async def test_card_still_in_the_ai_pipeline_enqueues_nothing(
    session_factory: SessionFactory,
) -> None:
    """Sin handoff no hay a quién validarle nada: el turno no derivó."""
    org_id, agent_id, conversation_id, _card = await _seed(
        session_factory, pipeline=stages.PIPELINE_IA, stage_name=stages.IA_QUALIFIED
    )
    await _add_turn(session_factory, org_id, agent_id, conversation_id, _photo("wamid.photo"))

    result, enqueued = await _trigger(session_factory, org_id, conversation_id, after_order=0)

    assert result == [] and enqueued == []


async def test_photos_answered_by_an_earlier_turn_are_not_picked_up(
    session_factory: SessionFactory,
) -> None:
    """Límite documentado: solo la media del turno que derivó. Una foto respondida antes
    sin handoff la valida el operador desde el panel."""
    org_id, agent_id, conversation_id, _card = await _seed(session_factory)
    old_photo = await _add_turn(
        session_factory, org_id, agent_id, conversation_id, _photo("wamid.old")
    )
    await _add_turn(
        session_factory, org_id, agent_id, conversation_id, {"role": "user", "content": "si"}
    )

    result, _ = await _trigger(session_factory, org_id, conversation_id, after_order=old_photo)

    assert result == []


async def test_text_only_turn_enqueues_nothing(session_factory: SessionFactory) -> None:
    """'Ya pagué' sin foto: el agente pide el comprobante, no hay nada que validar."""
    org_id, agent_id, conversation_id, _card = await _seed(session_factory)
    await _add_turn(
        session_factory, org_id, agent_id, conversation_id, {"role": "user", "content": "ya pagué"}
    )

    result, _ = await _trigger(session_factory, org_id, conversation_id, after_order=0)

    assert result == []


async def test_already_validated_receipt_is_not_enqueued_again(
    session_factory: SessionFactory,
) -> None:
    """Si el job del webhook sí llegó a correr, no se repite la lectura."""
    org_id, agent_id, conversation_id, card_id = await _seed(session_factory)
    await _add_turn(session_factory, org_id, agent_id, conversation_id, _photo("wamid.done"))
    await _add_turn(session_factory, org_id, agent_id, conversation_id, _photo("wamid.new"))
    async with session_factory() as session:
        session.add(
            PaymentReceipt(
                organization_id=org_id,
                card_id=card_id,
                wamid="wamid.done",
                image_sha256="sha-done",
                verdict="fail",
            )
        )
        await session.commit()

    result, enqueued = await _trigger(session_factory, org_id, conversation_id, after_order=0)

    assert result == ["wamid.new"]
    assert enqueued == ["wamid.new"]


async def test_document_receipts_count_as_media_too(session_factory: SessionFactory) -> None:
    org_id, agent_id, conversation_id, _card = await _seed(session_factory)
    await _add_turn(
        session_factory,
        org_id,
        agent_id,
        conversation_id,
        {
            "role": "user",
            "content": "[document: comprobante.pdf]",
            "wamid": "wamid.pdf",
            "media_type": "document",
        },
    )

    result, _ = await _trigger(session_factory, org_id, conversation_id, after_order=0)

    assert result == ["wamid.pdf"]
