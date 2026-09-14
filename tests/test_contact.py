"""Modelo Contact + columna conversation.full_name (issue #122, base de Contactos).

Capa de datos: persistencia, unique (organization_id, phone), aislamiento por
tenant y nullability de conversation.full_name. Sin servicio/endpoints (otros issues).
"""

from __future__ import annotations

import uuid

import pytest
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from server.modules.agent.domain.models import Contact, Conversation


async def test_contact_persists(db_session: AsyncSession) -> None:
    org = uuid.uuid4()
    contact = Contact(organization_id=org, phone="59178023135", full_name="Juan Pérez")
    db_session.add(contact)
    await db_session.commit()

    fetched = (await db_session.execute(select(Contact))).scalar_one()
    assert fetched.id is not None
    assert fetched.created_at is not None
    assert fetched.phone == "59178023135"
    assert fetched.full_name == "Juan Pérez"


async def test_contact_full_name_nullable(db_session: AsyncSession) -> None:
    contact = Contact(organization_id=uuid.uuid4(), phone="591700")
    db_session.add(contact)
    await db_session.commit()

    fetched = (await db_session.execute(select(Contact))).scalar_one()
    assert fetched.full_name is None


async def test_contact_unique_org_phone(db_session: AsyncSession) -> None:
    org = uuid.uuid4()
    db_session.add(Contact(organization_id=org, phone="591999"))
    db_session.add(Contact(organization_id=org, phone="591999"))
    with pytest.raises(IntegrityError):
        await db_session.commit()


async def test_contact_same_phone_other_org(db_session: AsyncSession) -> None:
    phone = "591888"
    db_session.add(Contact(organization_id=uuid.uuid4(), phone=phone))
    db_session.add(Contact(organization_id=uuid.uuid4(), phone=phone))
    await db_session.commit()  # distinta org → no colisiona

    count = len((await db_session.execute(select(Contact))).scalars().all())
    assert count == 2


async def test_conversation_full_name_null_and_value(db_session: AsyncSession) -> None:
    org = uuid.uuid4()
    without_name = Conversation(instance_id=uuid.uuid4(), organization_id=org, external_id="591777")
    with_name = Conversation(
        instance_id=uuid.uuid4(),
        organization_id=org,
        external_id="591666",
        full_name="María",
    )
    db_session.add_all([without_name, with_name])
    await db_session.commit()

    rows = (
        (await db_session.execute(select(Conversation).order_by(Conversation.external_id)))
        .scalars()
        .all()
    )
    by_ext = {c.external_id: c.full_name for c in rows}
    assert by_ext == {"591666": "María", "591777": None}
