"""Marcar una oportunidad como atendida (0036) + `last_activity_at`.

La señal "sin responder" salía del orden de los mensajes, así que toda conversación que
termina con un "gracias" del lead quedaba marcada para siempre. `attended_at` es la
salida explícita: cuenta como respuesta, y un inbound posterior la reenciende sola.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from server.modules.agent.domain.models import AiChatHistory, Conversation
from server.modules.crm.api.schemas import BoardOut, CardOut
from server.modules.crm.domain import attention
from server.modules.crm.domain.models import Card, Pipeline, Stage
from server.modules.crm.services.board_service import BoardService


class _NoopPublisher:
    async def publish(self, channel: str, payload: dict[str, object]) -> None:
        return None


def _at(minute: int) -> datetime:
    """Fecha fija en el pasado: `mark_attended` sella `now()`, y una conversación con
    timestamps futuros haría que atender quedara *antes* del último mensaje."""
    return datetime(2026, 7, 1, 12, minute, tzinfo=UTC)


def _naive(value: datetime | None) -> datetime | None:
    """SQLite devuelve los timestamps sin tzinfo (Postgres, con). Se comparan sin zona:
    lo que el test verifica es el instante, no cómo lo tipa el driver."""
    return value.replace(tzinfo=None) if value is not None else None


# --------------------------------------------------------------------------- dominio puro


def test_last_activity_is_the_newest_stamp_of_any_role() -> None:
    activity = attention.Activity(last_lead=_at(3), last_agent=_at(1), last_human=_at(5))
    assert attention.last_activity(activity) == _at(5)


def test_last_activity_is_none_without_messages() -> None:
    assert attention.last_activity(None) is None
    assert attention.last_activity(attention.Activity(None, None, None)) is None


def _awaiting(activity: attention.Activity | None, attended_at: datetime | None = None) -> bool:
    return attention.is_awaiting(
        activity, is_ai_active=False, closed_at=None, attended_at=attended_at
    )


def test_unanswered_lead_message_is_awaiting() -> None:
    assert _awaiting(attention.Activity(last_lead=_at(2), last_agent=_at(1), last_human=None))


def test_a_reply_after_the_lead_clears_it() -> None:
    assert not _awaiting(attention.Activity(last_lead=_at(1), last_agent=_at(2), last_human=None))
    assert not _awaiting(attention.Activity(last_lead=_at(1), last_agent=None, last_human=_at(2)))


def test_marking_attended_counts_as_an_answer() -> None:
    hanging = attention.Activity(last_lead=_at(2), last_agent=_at(1), last_human=None)
    assert _awaiting(hanging)
    assert not _awaiting(hanging, attended_at=_at(3))


def test_a_lead_message_after_attended_re_arms_the_signal() -> None:
    # Se marcó atendida en el minuto 3 y el lead volvió a escribir en el 4: vuelve a esperar,
    # sin que nadie tenga que deshacer la marca a mano.
    activity = attention.Activity(last_lead=_at(4), last_agent=_at(1), last_human=None)
    assert _awaiting(activity, attended_at=_at(3))


def test_ai_on_or_closed_opportunity_is_never_awaiting() -> None:
    hanging = attention.Activity(last_lead=_at(2), last_agent=_at(1), last_human=None)
    assert not attention.is_awaiting(hanging, is_ai_active=True, closed_at=None, attended_at=None)
    assert not attention.is_awaiting(
        hanging, is_ai_active=False, closed_at=_at(9), attended_at=None
    )


def test_a_lead_that_never_wrote_is_not_awaiting() -> None:
    assert not _awaiting(attention.Activity(last_lead=None, last_agent=None, last_human=None))


# ---------------------------------------------------------------------------- integración


async def _seed(
    session_factory: async_sessionmaker[AsyncSession], org_id: uuid.UUID
) -> tuple[uuid.UUID, uuid.UUID]:
    """Una card con la IA apagada y un inbound del lead sin responder."""
    async with session_factory() as session:
        pipeline = Pipeline(
            organization_id=org_id, kind="human", name="Gestión Postventa", position=0
        )
        session.add(pipeline)
        await session.flush()
        stage = Stage(pipeline_id=pipeline.id, name="Por atender", position=0, status_code="open")
        session.add(stage)
        await session.flush()
        conversation = Conversation(
            instance_id=uuid.uuid4(),
            organization_id=org_id,
            external_id="591700",
            full_name="Lead",
            is_ai_active=False,
        )
        session.add(conversation)
        await session.flush()
        card = Card(
            organization_id=org_id,
            conversation_id=conversation.id,
            stage_id=stage.id,
            title="Lead",
        )
        session.add(card)
        for role, at in (("assistant", _at(1)), ("user", _at(2))):
            session.add(
                AiChatHistory(
                    agent_id=uuid.uuid4(),
                    organization_id=org_id,
                    thread_id=str(conversation.id),
                    session_id=conversation.external_id,
                    message={"role": role, "content": "..."},
                    created_at=at,
                    message_order=at.minute,
                )
            )
        await session.commit()
        return card.id, conversation.id


async def _card_of(session_factory: async_sessionmaker[AsyncSession], org_id: uuid.UUID) -> CardOut:
    async with session_factory() as session:
        board: BoardOut = await BoardService(
            session=session,
            publisher=_NoopPublisher(),  # type: ignore[arg-type]
        ).get_board(org_id)
    return next(card for p in board.pipelines for s in p.stages for card in s.cards)


async def test_marking_attended_takes_the_card_out_of_the_queue(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    org_id = uuid.uuid4()
    card_id, _ = await _seed(session_factory, org_id)

    before = await _card_of(session_factory, org_id)
    assert before.awaiting_human is True
    assert before.attended_at is None
    # El board expone lo último que pasó en la conversación, no cuándo se creó la card.
    assert _naive(before.last_activity_at) == _naive(_at(2))

    async with session_factory() as session:
        marked = await BoardService(
            session=session,
            publisher=_NoopPublisher(),  # type: ignore[arg-type]
        ).mark_attended(card_id, org_id, uuid.uuid4())
    assert marked is True

    after = await _card_of(session_factory, org_id)
    assert after.awaiting_human is False
    assert after.attended_at is not None


async def test_attending_does_not_close_the_opportunity_nor_clear_its_flags(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """Un aviso de entrega es trabajo real y tiene su propio camino: atender no lo tapa."""
    org_id = uuid.uuid4()
    card_id, conversation_id = await _seed(session_factory, org_id)
    async with session_factory() as session:
        card = await session.get(Card, card_id)
        assert card is not None
        card.flags = ["receipt_review"]
        await session.commit()

    async with session_factory() as session:
        await BoardService(
            session=session,
            publisher=_NoopPublisher(),  # type: ignore[arg-type]
        ).mark_attended(card_id, org_id, uuid.uuid4())

    after = await _card_of(session_factory, org_id)
    assert after.flags == ["receipt_review"]
    async with session_factory() as session:
        conversation = await session.get(Conversation, conversation_id)
        assert conversation is not None
        assert conversation.closed_at is None


async def test_a_new_lead_message_after_attending_re_arms_the_signal(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    org_id = uuid.uuid4()
    card_id, conversation_id = await _seed(session_factory, org_id)
    async with session_factory() as session:
        await BoardService(
            session=session,
            publisher=_NoopPublisher(),  # type: ignore[arg-type]
        ).mark_attended(card_id, org_id, uuid.uuid4())
    assert (await _card_of(session_factory, org_id)).awaiting_human is False

    # El lead vuelve a escribir después de la marca: hay alguien esperando otra vez.
    async with session_factory() as session:
        card = await session.get(Card, card_id)
        assert card is not None and card.attended_at is not None
        session.add(
            AiChatHistory(
                agent_id=uuid.uuid4(),
                organization_id=org_id,
                thread_id=str(conversation_id),
                session_id="591700",
                message={"role": "user", "content": "una consulta más"},
                created_at=card.attended_at.replace(tzinfo=UTC) + (_at(1) - _at(0)),
                message_order=99,
            )
        )
        await session.commit()

    assert (await _card_of(session_factory, org_id)).awaiting_human is True


async def test_marking_a_card_of_another_org_is_rejected(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    org_id = uuid.uuid4()
    card_id, _ = await _seed(session_factory, org_id)
    async with session_factory() as session:
        marked = await BoardService(
            session=session,
            publisher=_NoopPublisher(),  # type: ignore[arg-type]
        ).mark_attended(card_id, uuid.uuid4(), uuid.uuid4())
    assert marked is False
    async with session_factory() as session:
        untouched = (await session.execute(select(Card.attended_at))).scalar_one()
        assert untouched is None
