"""ABM de oportunidad (#97): alta manual (crea conversación + card en el primer stage,
idempotente por teléfono), edición (nombre → conversation.full_name + card.title; notas)
y baja (borra la card + cierra la conversación → próximo inbound = oportunidad nueva).
Todo tenant-scoped.
"""

from __future__ import annotations

import uuid

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from server.modules.agent.domain.models import Agent, AgentInstance, Conversation, Product
from server.modules.crm.api.schemas import CardCreate, CardUpdate
from server.modules.crm.domain.models import Card, CardMove, Pipeline, Stage
from server.modules.crm.services.opportunity_service import OpportunityService

PHONE = "59171234567"


class _NoopPublisher:
    async def publish(self, channel: str, payload: dict[str, object]) -> None:
        return None


def _svc(session: AsyncSession) -> OpportunityService:
    return OpportunityService(session=session, publisher=_NoopPublisher())  # type: ignore[arg-type]


async def _seed_org_with_agent(
    session_factory: async_sessionmaker[AsyncSession], org_id: uuid.UUID
) -> None:
    """Product + Agent + AgentInstance activa + pipeline IA con 2 stages (Nuevo primero)."""
    async with session_factory() as session:
        product_slug = f"prod-{org_id.hex[:8]}"
        session.add(Product(slug=product_slug, display_name="Cursos"))
        await session.flush()
        agent = Agent(
            organization_id=org_id,
            product_slug=product_slug,
            display_name="Agente",
            system_prompt="sp",
            model="m",
        )
        session.add(agent)
        await session.flush()
        session.add(AgentInstance(agent_id=agent.id, display_name="inst", is_active=True))
        pipeline = Pipeline(organization_id=org_id, kind="ia", name="Gestión IA", position=0)
        session.add(pipeline)
        await session.flush()
        session.add_all(
            [
                Stage(pipeline_id=pipeline.id, name="Nuevo", position=0, status_code="open"),
                Stage(pipeline_id=pipeline.id, name="Calificando", position=1, status_code="open"),
            ]
        )
        await session.commit()


async def _seed_card(
    session_factory: async_sessionmaker[AsyncSession],
    org_id: uuid.UUID,
    *,
    full_name: str | None = "Original",
    with_move: bool = False,
) -> tuple[uuid.UUID, uuid.UUID]:
    """pipeline + stage + conversación + card directos. Devuelve (card_id, conv_id)."""
    async with session_factory() as session:
        pipeline = Pipeline(organization_id=org_id, kind="ia", name="Gestión IA", position=0)
        session.add(pipeline)
        await session.flush()
        stage = Stage(pipeline_id=pipeline.id, name="Nuevo", position=0, status_code="open")
        session.add(stage)
        await session.flush()
        conv = Conversation(
            instance_id=uuid.uuid4(),
            organization_id=org_id,
            external_id=PHONE,
            full_name=full_name,
        )
        session.add(conv)
        await session.flush()
        card = Card(
            organization_id=org_id,
            conversation_id=conv.id,
            stage_id=stage.id,
            title=full_name or PHONE,
        )
        session.add(card)
        await session.flush()
        if with_move:
            session.add(
                CardMove(
                    card_id=card.id, stage_from_id=None, stage_to_id=stage.id, moved_by="agent"
                )
            )
        await session.commit()
        return card.id, conv.id


# ---------- Alta ----------


async def test_create_builds_conversation_and_card_in_first_stage(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    org_id = uuid.uuid4()
    await _seed_org_with_agent(session_factory, org_id)

    async with session_factory() as s:
        out = await _svc(s).create(org_id, CardCreate(phone=PHONE, full_name="Ada"), uuid.uuid4())

    assert out.title == "Ada"
    assert out.phone == PHONE
    async with session_factory() as s:
        conv = (
            await s.execute(select(Conversation).where(Conversation.external_id == PHONE))
        ).scalar_one()
        assert conv.full_name == "Ada"
        card = (await s.execute(select(Card).where(Card.conversation_id == conv.id))).scalar_one()
        stage = await s.get(Stage, card.stage_id)
        assert stage is not None and stage.name == "Nuevo"  # primer stage
        moves = (
            (await s.execute(select(CardMove).where(CardMove.card_id == card.id))).scalars().all()
        )
        assert len(moves) == 1 and moves[0].stage_from_id is None


async def test_create_is_idempotent_by_phone(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    org_id = uuid.uuid4()
    await _seed_org_with_agent(session_factory, org_id)

    async with session_factory() as s:
        a = await _svc(s).create(org_id, CardCreate(phone=PHONE, full_name="Ada"), uuid.uuid4())
    async with session_factory() as s:
        b = await _svc(s).create(org_id, CardCreate(phone=PHONE, full_name="Otra"), uuid.uuid4())

    assert b.id == a.id
    async with session_factory() as s:
        cards = (await s.execute(select(Card))).scalars().all()
        assert len(cards) == 1  # sin duplicar


async def test_create_without_name_titles_phone(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    org_id = uuid.uuid4()
    await _seed_org_with_agent(session_factory, org_id)

    async with session_factory() as s:
        out = await _svc(s).create(org_id, CardCreate(phone=PHONE), uuid.uuid4())

    assert out.title == PHONE


async def test_create_without_agent_raises(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    org_id = uuid.uuid4()
    # Sólo pipeline, sin Agent/AgentInstance.
    async with session_factory() as session:
        pipeline = Pipeline(organization_id=org_id, kind="ia", name="Gestión IA", position=0)
        session.add(pipeline)
        await session.flush()
        session.add(Stage(pipeline_id=pipeline.id, name="Nuevo", position=0, status_code="open"))
        await session.commit()

    async with session_factory() as s:
        with pytest.raises(ValueError, match="agente"):
            await _svc(s).create(org_id, CardCreate(phone=PHONE), uuid.uuid4())


# ---------- Edición ----------


async def test_update_sets_name_and_notes(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    org_id = uuid.uuid4()
    card_id, conv_id = await _seed_card(session_factory, org_id, full_name="jesus es mi pastor")

    async with session_factory() as s:
        out = await _svc(s).update(
            card_id, org_id, CardUpdate(full_name="Andrés Pérez", notes="cliente enojado")
        )

    assert out is not None and out.title == "Andrés Pérez"
    async with session_factory() as s:
        card = await s.get(Card, card_id)
        conv = await s.get(Conversation, conv_id)
        assert card is not None and card.notes == "cliente enojado" and card.title == "Andrés Pérez"
        assert conv is not None and conv.full_name == "Andrés Pérez"


async def test_update_omitting_a_field_leaves_it_untouched(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    org_id = uuid.uuid4()
    card_id, conv_id = await _seed_card(session_factory, org_id, full_name="Original")

    # Sólo notas: el nombre (full_name/title) no se toca (exclude_unset).
    async with session_factory() as s:
        await _svc(s).update(card_id, org_id, CardUpdate(notes="cambió la fecha de la boda"))

    async with session_factory() as s:
        card = await s.get(Card, card_id)
        conv = await s.get(Conversation, conv_id)
        assert card is not None and card.notes == "cambió la fecha de la boda"
        assert card.title == "Original"  # intacto
        assert conv is not None and conv.full_name == "Original"  # intacto


# ---------- Baja ----------


async def test_delete_removes_card_and_closes_conversation(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    org_id = uuid.uuid4()
    card_id, conv_id = await _seed_card(session_factory, org_id, with_move=True)

    async with session_factory() as s:
        ok = await _svc(s).delete(card_id, org_id)

    assert ok is True
    async with session_factory() as s:
        assert await s.get(Card, card_id) is None
        moves = (
            (await s.execute(select(CardMove).where(CardMove.card_id == card_id))).scalars().all()
        )
        assert moves == []
        conv = await s.get(Conversation, conv_id)
        # La conversación queda como registro, pero CERRADA: el webhook no la reusa, el
        # próximo inbound del lead abre una oportunidad nueva y limpia (#163).
        assert conv is not None
        assert conv.closed_at is not None


async def test_update_and_delete_are_tenant_scoped(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    org_id = uuid.uuid4()
    card_id, _conv = await _seed_card(session_factory, org_id)
    other_org = uuid.uuid4()

    async with session_factory() as s:
        assert await _svc(s).update(card_id, other_org, CardUpdate(notes="x")) is None
        assert await _svc(s).delete(card_id, other_org) is False
    # La card sigue intacta para su org.
    async with session_factory() as s:
        assert await s.get(Card, card_id) is not None
