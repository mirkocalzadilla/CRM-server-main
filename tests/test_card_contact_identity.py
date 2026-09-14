"""Resolución de identidad card↔contacto por teléfono.

Una card creada cuando el contacto ya existe nace linkeada (`contact_id`) y titulada
con su nombre; el board y el detalle resuelven el título como `conversation.full_name
→ contact.full_name → phone`; y ganar una oportunidad linkea TODAS las cards del
teléfono (no solo la ganada).
"""

from __future__ import annotations

import uuid

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from server.modules.agent.domain.models import Agent, AgentInstance, Conversation, Product
from server.modules.agent.repositories.contact_repository import ContactRepository
from server.modules.crm.api.schemas import CardCreate
from server.modules.crm.domain.models import Card, Pipeline, Stage
from server.modules.crm.services.board_service import BoardService
from server.modules.crm.services.card_service import CardService
from server.modules.crm.services.opportunity_service import OpportunityService

PHONE = "59171234567"
CONTACT_NAME = "Diego Gandarillas"


class _NoopPublisher:
    async def publish(self, channel: str, payload: dict[str, object]) -> None:
        return None


async def _seed_org(
    session_factory: async_sessionmaker[AsyncSession], org_id: uuid.UUID
) -> tuple[uuid.UUID, uuid.UUID]:
    """Product + Agent + instancia activa + pipeline IA (Nuevo → Ganado).
    Devuelve (start_stage_id, won_stage_id)."""
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
        start = Stage(pipeline_id=pipeline.id, name="Nuevo", position=0, status_code="open")
        won = Stage(pipeline_id=pipeline.id, name="Ganado", position=1, status_code="won")
        session.add_all([start, won])
        await session.commit()
        return start.id, won.id


async def _seed_contact(
    session_factory: async_sessionmaker[AsyncSession],
    org_id: uuid.UUID,
    *,
    full_name: str | None = CONTACT_NAME,
) -> uuid.UUID:
    async with session_factory() as session:
        contact = await ContactRepository(session).upsert(org_id, PHONE, full_name)
        await session.commit()
        return contact.id


async def _seed_conversation(
    session_factory: async_sessionmaker[AsyncSession],
    org_id: uuid.UUID,
    *,
    full_name: str | None = None,
) -> uuid.UUID:
    async with session_factory() as session:
        conv = Conversation(
            instance_id=uuid.uuid4(),
            organization_id=org_id,
            external_id=PHONE,
            full_name=full_name,
        )
        session.add(conv)
        await session.commit()
        return conv.id


async def _get_card(
    session_factory: async_sessionmaker[AsyncSession], conversation_id: uuid.UUID
) -> Card | None:
    async with session_factory() as session:
        result = await session.execute(select(Card).where(Card.conversation_id == conversation_id))
        return result.scalar_one_or_none()


def _board_svc(session: AsyncSession) -> BoardService:
    return BoardService(session=session, publisher=_NoopPublisher())  # type: ignore[arg-type]


async def test_manual_opportunity_links_existing_contact(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    org_id = uuid.uuid4()
    await _seed_org(session_factory, org_id)
    contact_id = await _seed_contact(session_factory, org_id)

    async with session_factory() as session:
        svc = OpportunityService(session=session, publisher=_NoopPublisher())  # type: ignore[arg-type]
        out = await svc.create(org_id, CardCreate(phone=PHONE), created_by=uuid.uuid4())

    assert out.title == CONTACT_NAME  # no nació con el wa_id crudo
    card = await _get_card(session_factory, out.conversation_id)
    assert card is not None and card.contact_id == contact_id


async def test_agent_sync_links_existing_contact(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    org_id = uuid.uuid4()
    await _seed_org(session_factory, org_id)
    contact_id = await _seed_contact(session_factory, org_id)
    conv_id = await _seed_conversation(session_factory, org_id)

    async with session_factory() as session:
        svc = CardService(session=session, publisher=_NoopPublisher())  # type: ignore[arg-type]
        await svc.sync(conv_id, org_id)

    card = await _get_card(session_factory, conv_id)
    assert card is not None
    assert card.contact_id == contact_id
    assert card.title == CONTACT_NAME


async def test_board_and_detail_resolve_title_from_contact(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    org_id = uuid.uuid4()
    await _seed_org(session_factory, org_id)
    await _seed_contact(session_factory, org_id)
    conv_id = await _seed_conversation(session_factory, org_id)
    async with session_factory() as session:
        await CardService(session=session, publisher=_NoopPublisher()).sync(conv_id, org_id)  # type: ignore[arg-type]

    card = await _get_card(session_factory, conv_id)
    assert card is not None
    async with session_factory() as session:
        board = await _board_svc(session).get_board(org_id)
        detail = await _board_svc(session).get_card_detail(card.id, org_id)

    titles = [c.title for p in board.pipelines for s in p.stages for c in s.cards]
    assert titles == [CONTACT_NAME]
    assert detail is not None
    assert detail.title == CONTACT_NAME
    assert detail.full_name is None  # el prefill de edición sigue siendo el de la conversación


async def test_conversation_name_beats_contact_name(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    org_id = uuid.uuid4()
    await _seed_org(session_factory, org_id)
    await _seed_contact(session_factory, org_id)
    conv_id = await _seed_conversation(session_factory, org_id, full_name="Pepe")
    async with session_factory() as session:
        await CardService(session=session, publisher=_NoopPublisher()).sync(conv_id, org_id)  # type: ignore[arg-type]

    card = await _get_card(session_factory, conv_id)
    assert card is not None
    async with session_factory() as session:
        detail = await _board_svc(session).get_card_detail(card.id, org_id)
    assert detail is not None and detail.title == "Pepe"


async def test_won_hook_links_all_cards_of_phone(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    org_id = uuid.uuid4()
    _start_id, won_id = await _seed_org(session_factory, org_id)
    conv_a = await _seed_conversation(session_factory, org_id, full_name="Juan")
    conv_b = await _seed_conversation(session_factory, org_id)
    async with session_factory() as session:
        svc = CardService(session=session, publisher=_NoopPublisher())  # type: ignore[arg-type]
        await svc.sync(conv_a, org_id)
        await svc.sync(conv_b, org_id)

    card_a = await _get_card(session_factory, conv_a)
    assert card_a is not None
    async with session_factory() as session:
        await _board_svc(session).move_card(card_a.id, won_id, uuid.uuid4(), org_id)

    linked_a = await _get_card(session_factory, conv_a)
    linked_b = await _get_card(session_factory, conv_b)
    assert linked_a is not None and linked_a.contact_id is not None
    # La card hermana (misma phone, otra conversación) quedó linkeada al mismo contacto.
    assert linked_b is not None and linked_b.contact_id == linked_a.contact_id
