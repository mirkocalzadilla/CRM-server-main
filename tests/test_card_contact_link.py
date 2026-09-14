"""Vínculo card↔contacto vía FK `card.contact_id` (#139).

Cubre los tres puntos donde se setea/lee el link (la misma semántica que el backfill
de la migración): alta manual de contacto desde el detalle (linkea la card por phone,
idempotente), hook 'won' (la card ganada queda linkeada) y la resolución del contacto
en `CardDetailOut`. Más el aislamiento multi-tenant (no linkea cross-org).
"""

from __future__ import annotations

import uuid

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from server.modules.agent.domain.models import Contact, Conversation
from server.modules.crm.api.schemas import CardDetailOut, ContactCreate
from server.modules.crm.domain.models import Card, Pipeline, Stage
from server.modules.crm.services.board_service import BoardService
from server.modules.crm.services.contact_service import ContactService

PHONE = "59171234567"


class _NoopPublisher:
    async def publish(self, channel: str, payload: dict[str, object]) -> None:
        return None


async def _seed(
    session_factory: async_sessionmaker[AsyncSession],
    org_id: uuid.UUID,
    *,
    phone: str = PHONE,
    full_name: str | None = None,
) -> tuple[uuid.UUID, uuid.UUID]:
    """pipeline (open + won) + conversación + card en el stage open, sin contacto.
    Devuelve (card_id, won_stage_id)."""
    async with session_factory() as session:
        pipeline = Pipeline(organization_id=org_id, kind="ia", name="Gestión IA", position=0)
        session.add(pipeline)
        await session.flush()
        start = Stage(pipeline_id=pipeline.id, name="Enganchando", position=0, status_code="open")
        won = Stage(pipeline_id=pipeline.id, name="Ganado", position=1, status_code="won")
        session.add_all([start, won])
        await session.flush()
        conv = Conversation(
            instance_id=uuid.uuid4(),
            organization_id=org_id,
            external_id=phone,
            full_name=full_name,
        )
        session.add(conv)
        await session.flush()
        card = Card(
            organization_id=org_id, conversation_id=conv.id, stage_id=start.id, title="Lead"
        )
        session.add(card)
        await session.commit()
        return card.id, won.id


async def _detail(
    session_factory: async_sessionmaker[AsyncSession], card_id: uuid.UUID, org_id: uuid.UUID
) -> CardDetailOut | None:
    async with session_factory() as session:
        svc = BoardService(session=session, publisher=_NoopPublisher())  # type: ignore[arg-type]
        return await svc.get_card_detail(card_id, org_id)


async def _create_contact(
    session_factory: async_sessionmaker[AsyncSession],
    org_id: uuid.UUID,
    *,
    phone: str = PHONE,
    full_name: str = "Lead",  # el ABM exige nombre (no hay contacto sin nombre)
) -> uuid.UUID:
    async with session_factory() as session:
        contact = await ContactService(session).create(
            org_id, ContactCreate(phone=phone, full_name=full_name)
        )
        await session.commit()
        return contact.id


async def _contacts(
    session_factory: async_sessionmaker[AsyncSession], org_id: uuid.UUID
) -> list[Contact]:
    async with session_factory() as session:
        result = await session.execute(select(Contact).where(Contact.organization_id == org_id))
        return list(result.scalars().all())


async def test_detail_contact_is_null_without_contact(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    org_id = uuid.uuid4()
    card_id, _won = await _seed(session_factory, org_id)

    detail = await _detail(session_factory, card_id, org_id)

    assert detail is not None
    assert detail.contact is None


async def test_manual_contact_creation_links_card_by_phone(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    org_id = uuid.uuid4()
    card_id, _won = await _seed(session_factory, org_id)

    contact_id = await _create_contact(session_factory, org_id, full_name="Ada")

    detail = await _detail(session_factory, card_id, org_id)
    assert detail is not None
    assert detail.contact is not None
    assert detail.contact.id == contact_id
    assert detail.contact.full_name == "Ada"


async def test_manual_contact_creation_is_idempotent_and_keeps_link(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    org_id = uuid.uuid4()
    card_id, _won = await _seed(session_factory, org_id)

    first = await _create_contact(session_factory, org_id)
    second = await _create_contact(session_factory, org_id, full_name="Grace")

    assert second == first  # mismo contacto, sin duplicar
    assert len(await _contacts(session_factory, org_id)) == 1
    detail = await _detail(session_factory, card_id, org_id)
    assert detail is not None and detail.contact is not None
    assert detail.contact.id == first
    assert detail.contact.full_name == "Grace"


async def test_won_hook_links_card_to_contact(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    org_id = uuid.uuid4()
    card_id, won_id = await _seed(session_factory, org_id, full_name="Juan")

    async with session_factory() as session:
        svc = BoardService(session=session, publisher=_NoopPublisher())  # type: ignore[arg-type]
        await svc.move_card(card_id, won_id, uuid.uuid4(), org_id)

    contacts = await _contacts(session_factory, org_id)
    assert len(contacts) == 1
    detail = await _detail(session_factory, card_id, org_id)
    assert detail is not None and detail.contact is not None
    assert detail.contact.id == contacts[0].id
    assert detail.contact.full_name == "Juan"


async def test_link_is_tenant_scoped(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    org_a, org_b = uuid.uuid4(), uuid.uuid4()
    card_a, _won_a = await _seed(session_factory, org_a)
    card_b, _won_b = await _seed(session_factory, org_b)  # mismo phone, otra org

    await _create_contact(session_factory, org_a, full_name="Solo A")

    detail_a = await _detail(session_factory, card_a, org_a)
    detail_b = await _detail(session_factory, card_b, org_b)
    assert detail_a is not None and detail_a.contact is not None
    assert detail_b is not None and detail_b.contact is None  # sin fuga cross-tenant
