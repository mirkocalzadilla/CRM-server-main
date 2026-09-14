"""Hook de 'won' (#101): al mover una card a un stage `status_code == "won"`, se
crea/actualiza el contacto del lead (idempotente por org+phone), en la misma
transacción que el move. Stages no-'won' no tocan contactos. Ganar sin nombre
se rechaza (#241): no puede haber contactos "Sin nombre".
"""

from __future__ import annotations

import uuid

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from server.modules.agent.domain.models import Contact, Conversation
from server.modules.crm.domain.models import Card, Pipeline, Stage
from server.modules.crm.services.board_service import BoardService
from server.shared.exceptions import WonRequiresNameError

PHONE = "59171234567"


class _NoopPublisher:
    async def publish(self, channel: str, payload: dict[str, object]) -> None:
        return None


async def _contacts(
    session_factory: async_sessionmaker[AsyncSession], org_id: uuid.UUID
) -> list[Contact]:
    async with session_factory() as session:
        result = await session.execute(select(Contact).where(Contact.organization_id == org_id))
        return list(result.scalars().all())


async def _seed(
    session_factory: async_sessionmaker[AsyncSession],
    org_id: uuid.UUID,
    *,
    full_name: str | None = None,
) -> tuple[uuid.UUID, uuid.UUID, uuid.UUID, uuid.UUID]:
    """pipeline con 3 stages (Enganchando/Calificando = open, Ganado = won) + conversación
    + card en Enganchando. Devuelve (card_id, won_stage_id, other_open_stage_id, conv_id)."""
    async with session_factory() as session:
        pipeline = Pipeline(organization_id=org_id, kind="ia", name="Gestión IA", position=0)
        session.add(pipeline)
        await session.flush()
        start = Stage(pipeline_id=pipeline.id, name="Enganchando", position=0, status_code="open")
        other = Stage(pipeline_id=pipeline.id, name="Calificando", position=1, status_code="open")
        won = Stage(pipeline_id=pipeline.id, name="Ganado", position=2, status_code="won")
        session.add_all([start, other, won])
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
            stage_id=start.id,
            title="Lead",
        )
        session.add(card)
        await session.commit()
        return card.id, won.id, other.id, conv.id


async def _move(
    session_factory: async_sessionmaker[AsyncSession],
    card_id: uuid.UUID,
    stage_id: uuid.UUID,
    org_id: uuid.UUID,
) -> None:
    async with session_factory() as session:
        svc = BoardService(session=session, publisher=_NoopPublisher())  # type: ignore[arg-type]
        await svc.move_card(card_id, stage_id, uuid.uuid4(), org_id)


async def _set_conv_name(
    session_factory: async_sessionmaker[AsyncSession], conv_id: uuid.UUID, name: str
) -> None:
    async with session_factory() as session:
        conv = await session.get(Conversation, conv_id)
        assert conv is not None
        conv.full_name = name
        await session.commit()


async def test_move_to_won_creates_contact(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    org_id = uuid.uuid4()
    card_id, won_id, _other, _conv = await _seed(session_factory, org_id, full_name="Juan Lead")

    await _move(session_factory, card_id, won_id, org_id)

    contacts = await _contacts(session_factory, org_id)
    assert len(contacts) == 1
    assert contacts[0].phone == PHONE
    assert contacts[0].full_name == "Juan Lead"


async def test_move_to_non_won_does_not_create_contact(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    org_id = uuid.uuid4()
    card_id, _won, other_id, _conv = await _seed(session_factory, org_id, full_name="Juan")

    await _move(session_factory, card_id, other_id, org_id)

    assert await _contacts(session_factory, org_id) == []


async def test_won_hook_is_idempotent_and_updates_name(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    org_id = uuid.uuid4()
    card_id, won_id, other_id, conv_id = await _seed(session_factory, org_id, full_name="Juan")

    await _move(session_factory, card_id, won_id, org_id)
    first = await _contacts(session_factory, org_id)
    assert len(first) == 1
    assert first[0].full_name == "Juan"

    # Se corrige el nombre y se re-cierra (won→open→won): no duplica, actualiza el nombre.
    await _set_conv_name(session_factory, conv_id, "Juan Tardío")
    await _move(session_factory, card_id, other_id, org_id)
    await _move(session_factory, card_id, won_id, org_id)

    again = await _contacts(session_factory, org_id)
    assert len(again) == 1  # idempotente por org+phone
    assert again[0].id == first[0].id
    assert again[0].full_name == "Juan Tardío"


async def test_move_to_won_without_name_is_rejected(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    # Lead sin nombre y sin contacto previo: ganar se rechaza y nada cambia (#241).
    org_id = uuid.uuid4()
    card_id, won_id, _other, _conv = await _seed(session_factory, org_id, full_name=None)

    with pytest.raises(WonRequiresNameError):
        await _move(session_factory, card_id, won_id, org_id)

    assert await _contacts(session_factory, org_id) == []
    async with session_factory() as session:
        card = await session.get(Card, card_id)
        assert card is not None
        assert card.stage_id != won_id  # el move no se aplicó


async def test_move_to_won_without_name_allows_if_contact_already_named(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    # La conversación no tiene nombre, pero el teléfono ya es un contacto con nombre
    # (ej. lead que vuelve): ganar está permitido y el nombre existente se preserva.
    org_id = uuid.uuid4()
    card_id, won_id, _other, _conv = await _seed(session_factory, org_id, full_name=None)
    async with session_factory() as session:
        session.add(Contact(organization_id=org_id, phone=PHONE, full_name="Ada Previa"))
        await session.commit()

    await _move(session_factory, card_id, won_id, org_id)

    contacts = await _contacts(session_factory, org_id)
    assert len(contacts) == 1
    assert contacts[0].full_name == "Ada Previa"
