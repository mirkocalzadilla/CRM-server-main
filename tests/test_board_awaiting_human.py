"""Señal "sin responder" en la card del board (read-model, sin migración).

Con la IA apagada (takeover) un inbound del lead sin respuesta humana/agente posterior
marca `awaiting_human=True`: hay alguien esperando y nadie contestó. Con la IA activa el
agente responde solo, así que nunca se evalúa. Deriva del último timestamp por rol en
`ai_chat_histories` vs la última respuesta humana en `app_chat_histories`.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from server.modules.agent.domain.models import AiChatHistory, AppChatHistory, Conversation
from server.modules.crm.api.schemas import BoardOut, CardOut
from server.modules.crm.domain.models import Card, Pipeline, Stage
from server.modules.crm.services.board_service import BoardService


class _NoopPublisher:
    async def publish(self, channel: str, payload: dict[str, object]) -> None:
        return None


def _cards(board: BoardOut) -> list[CardOut]:
    return [card for p in board.pipelines for s in p.stages for card in s.cards]


def _at(minute: int) -> datetime:
    return datetime(2026, 7, 1, 12, minute, tzinfo=UTC)


async def _seed_stage(
    session_factory: async_sessionmaker[AsyncSession], org_id: uuid.UUID
) -> uuid.UUID:
    async with session_factory() as session:
        pipeline = Pipeline(organization_id=org_id, kind="human", name="Gestión Humana", position=0)
        session.add(pipeline)
        await session.flush()
        stage = Stage(pipeline_id=pipeline.id, name="Por atender", position=0, status_code="open")
        session.add(stage)
        await session.commit()
        return stage.id


async def _seed_conversation_card(
    session: AsyncSession,
    org_id: uuid.UUID,
    stage_id: uuid.UUID,
    *,
    title: str,
    phone: str,
    is_ai_active: bool,
    closed: bool = False,
) -> Conversation:
    # full_name = title: la proyección del board resuelve el título desde la conversación.
    conversation = Conversation(
        instance_id=uuid.uuid4(),
        organization_id=org_id,
        external_id=phone,
        full_name=title,
        is_ai_active=is_ai_active,
        closed_at=_at(0) if closed else None,
    )
    session.add(conversation)
    await session.flush()
    session.add(
        Card(
            organization_id=org_id, conversation_id=conversation.id, stage_id=stage_id, title=title
        )
    )
    return conversation


def _ai_turn(conv: Conversation, org_id: uuid.UUID, role: str, at: datetime) -> AiChatHistory:
    # message_order es BIGSERIAL en Postgres; en SQLite no hay secuencia, así que se setea
    # explícito (el minuto alcanza para un orden estable dentro del test).
    return AiChatHistory(
        agent_id=uuid.uuid4(),
        organization_id=org_id,
        thread_id=str(conv.id),
        session_id=conv.external_id,
        message={"role": role, "content": "..."},
        created_at=at,
        message_order=at.minute,
    )


async def _board_of(
    session_factory: async_sessionmaker[AsyncSession], org_id: uuid.UUID
) -> BoardOut:
    async with session_factory() as session:
        return await BoardService(
            session=session,
            publisher=_NoopPublisher(),  # type: ignore[arg-type]
        ).get_board(org_id)


async def test_board_flags_only_unanswered_ia_off_cards(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    org_id = uuid.uuid4()
    stage_id = await _seed_stage(session_factory, org_id)

    async with session_factory() as session:
        # 1) IA off, último turno = inbound del lead sin respuesta → sin responder
        pending = await _seed_conversation_card(
            session, org_id, stage_id, title="Esperando", phone="111", is_ai_active=False
        )
        session.add(_ai_turn(pending, org_id, "assistant", _at(1)))
        session.add(_ai_turn(pending, org_id, "user", _at(2)))

        # 2) IA off, un humano ya respondió después del inbound → atendida
        human = await _seed_conversation_card(
            session, org_id, stage_id, title="Humano respondió", phone="222", is_ai_active=False
        )
        session.add(_ai_turn(human, org_id, "user", _at(1)))
        session.add(
            AppChatHistory(
                agent_id=uuid.uuid4(),
                organization_id=org_id,
                session_id="222",
                sender=str(uuid.uuid4()),
                message="ya te contesto",
                message_time=_at(2),
            )
        )

        # 3) IA off, el agente respondió después del inbound → atendida
        answered = await _seed_conversation_card(
            session, org_id, stage_id, title="Agente respondió", phone="333", is_ai_active=False
        )
        session.add(_ai_turn(answered, org_id, "user", _at(1)))
        session.add(_ai_turn(answered, org_id, "assistant", _at(2)))

        # 4) IA activa con inbound colgado → no se evalúa (el agente responde solo)
        ai_on = await _seed_conversation_card(
            session, org_id, stage_id, title="IA activa", phone="444", is_ai_active=True
        )
        session.add(_ai_turn(ai_on, org_id, "user", _at(2)))

        # 5) IA off sin historial → nada que responder
        await _seed_conversation_card(
            session, org_id, stage_id, title="Sin mensajes", phone="555", is_ai_active=False
        )

        # 6) IA off, inbound colgado, pero oportunidad CERRADA (won/lost) → negocio muerto,
        # no se marca "sin responder" aunque el último turno sea del lead.
        closed = await _seed_conversation_card(
            session, org_id, stage_id, title="Cerrada", phone="666", is_ai_active=False, closed=True
        )
        session.add(_ai_turn(closed, org_id, "user", _at(2)))
        await session.commit()

    awaiting = {c.title: c.awaiting_human for c in _cards(await _board_of(session_factory, org_id))}

    assert awaiting == {
        "Esperando": True,
        "Humano respondió": False,
        "Agente respondió": False,
        "IA activa": False,
        "Sin mensajes": False,
        "Cerrada": False,
    }


async def test_board_exposes_is_ai_active(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    org_id = uuid.uuid4()
    stage_id = await _seed_stage(session_factory, org_id)
    async with session_factory() as session:
        await _seed_conversation_card(
            session, org_id, stage_id, title="Takeover", phone="111", is_ai_active=False
        )
        await _seed_conversation_card(
            session, org_id, stage_id, title="Con IA", phone="222", is_ai_active=True
        )
        await session.commit()

    states = {c.title: c.is_ai_active for c in _cards(await _board_of(session_factory, org_id))}

    assert states == {"Takeover": False, "Con IA": True}
