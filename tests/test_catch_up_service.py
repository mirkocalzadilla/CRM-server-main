"""Catch-up de inbounds sin atender (#187): re-encola solo las conversaciones
AI-elegibles con un inbound del lead por encima del high-water mark
`answered_through_order` (#240). Seed mínimo sobre SQLite.

`message_order` es BIGSERIAL en Postgres (`FetchedValue`); en SQLite no hay secuencia,
así que se setea explícito para fijar el orden de los turnos.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from server.modules.agent.domain.models import (
    Agent,
    AgentInstance,
    AiChatHistory,
    Conversation,
)
from server.modules.agent.repositories.conversation_repository import ConversationRepository
from server.modules.agent.services.catch_up_service import run_catch_up


class _FakeDispatcher:
    """Registra (conversation_id, tenant_id) re-encolados; sin Redis."""

    def __init__(self) -> None:
        self.enqueued: list[tuple[uuid.UUID, uuid.UUID]] = []

    async def enqueue(self, conversation_id: uuid.UUID, tenant_id: uuid.UUID) -> None:
        self.enqueued.append((conversation_id, tenant_id))


async def _seed_org(session: AsyncSession) -> tuple[uuid.UUID, AgentInstance]:
    org_id = uuid.uuid4()
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
    return org_id, instance


async def _add_conversation(
    session: AsyncSession,
    instance: AgentInstance,
    org_id: uuid.UUID,
    *,
    external_id: str,
    is_ai_active: bool = True,
    closed: bool = False,
    answered_through_order: int = 0,
) -> Conversation:
    conv = await ConversationRepository(session).add(
        Conversation(
            instance_id=instance.id,
            organization_id=org_id,
            external_id=external_id,
            is_ai_active=is_ai_active,
            closed_at=datetime.now(UTC) if closed else None,
            answered_through_order=answered_through_order,
        )
    )
    return conv


async def _add_turn(
    session: AsyncSession,
    conv: Conversation,
    agent_id: uuid.UUID,
    org_id: uuid.UUID,
    *,
    role: str,
    order: int,
) -> None:
    session.add(
        AiChatHistory(
            agent_id=agent_id,
            organization_id=org_id,
            thread_id=str(conv.id),
            session_id=conv.external_id,
            message={"role": role, "content": "..."},
            message_order=order,
        )
    )
    await session.flush()


async def test_catch_up_requeues_only_unanswered_eligible(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    async with session_factory() as session:
        org_id, instance = await _seed_org(session)
        agent_id = instance.agent_id

        # 1) sin atender + elegible → se re-encola (inbound del lead sobre el mark)
        pending = await _add_conversation(session, instance, org_id, external_id="111")
        await _add_turn(session, pending, agent_id, org_id, role="assistant", order=1)
        await _add_turn(session, pending, agent_id, org_id, role="user", order=2)

        # 2) ya respondida (mark al día) → se saltea
        answered = await _add_conversation(
            session, instance, org_id, external_id="222", answered_through_order=1
        )
        await _add_turn(session, answered, agent_id, org_id, role="user", order=1)
        await _add_turn(session, answered, agent_id, org_id, role="assistant", order=2)

        # 3) silenciada (handoff, is_ai_active=False) con inbound colgado → NO es de la IA
        silenced = await _add_conversation(
            session, instance, org_id, external_id="333", is_ai_active=False
        )
        await _add_turn(session, silenced, agent_id, org_id, role="user", order=1)

        # 4) cerrada (closed_at) con inbound colgado → oportunidad muerta, no se toca
        closed = await _add_conversation(session, instance, org_id, external_id="444", closed=True)
        await _add_turn(session, closed, agent_id, org_id, role="user", order=1)

        # 5) sin historial → nada que contestar
        await _add_conversation(session, instance, org_id, external_id="555")

        # 6) carrera #240: el lead escribió (orden 2) con el turno anterior en vuelo, cuya
        # respuesta se guardó tarde (orden 3, por encima). El viejo "última fila = user"
        # la salteaba; el mark (1 < 2) la re-encola.
        race = await _add_conversation(
            session, instance, org_id, external_id="666", answered_through_order=1
        )
        await _add_turn(session, race, agent_id, org_id, role="user", order=1)
        await _add_turn(session, race, agent_id, org_id, role="user", order=2)
        await _add_turn(session, race, agent_id, org_id, role="assistant", order=3)

        await session.commit()
        pending_id, race_id = pending.id, race.id

        dispatcher = _FakeDispatcher()
        requeued = await run_catch_up(session, dispatcher)

    assert requeued == 2
    assert set(dispatcher.enqueued) == {(pending_id, org_id), (race_id, org_id)}


async def test_catch_up_noop_when_all_answered(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    async with session_factory() as session:
        org_id, instance = await _seed_org(session)
        agent_id = instance.agent_id
        conv = await _add_conversation(
            session, instance, org_id, external_id="111", answered_through_order=1
        )
        await _add_turn(session, conv, agent_id, org_id, role="user", order=1)
        await _add_turn(session, conv, agent_id, org_id, role="assistant", order=2)
        await session.commit()

        dispatcher = _FakeDispatcher()
        requeued = await run_catch_up(session, dispatcher)

    assert requeued == 0
    assert dispatcher.enqueued == []
