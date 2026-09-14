"""Board read-model: expone `phone` (external_id) por card para la búsqueda por número (#57).

El LEFT JOIN a `conversation` garantiza que una card sin conversación igual aparezca
en el tablero (con `phone` vacío) en vez de desaparecer.
"""

from __future__ import annotations

import uuid

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from server.modules.agent.domain.models import Conversation
from server.modules.crm.api.schemas import BoardOut, CardOut
from server.modules.crm.domain.models import Card, Pipeline, Stage
from server.modules.crm.services.board_service import BoardService


class _NoopPublisher:
    async def publish(self, channel: str, payload: dict[str, object]) -> None:
        return None


def _cards(board: BoardOut) -> list[CardOut]:
    return [card for p in board.pipelines for s in p.stages for card in s.cards]


async def _seed_card(
    session_factory: async_sessionmaker[AsyncSession],
    org_id: uuid.UUID,
    *,
    external_id: str | None,
    title: str = "Lead",
) -> None:
    """Siembra pipeline + stage + card; con conversación sólo si `external_id` no es None."""
    async with session_factory() as session:
        pipeline = Pipeline(organization_id=org_id, kind="ia", name="Gestión IA", position=0)
        session.add(pipeline)
        await session.flush()
        stage = Stage(pipeline_id=pipeline.id, name="Nuevo", position=0, status_code="open")
        session.add(stage)
        await session.flush()
        if external_id is not None:
            conversation = Conversation(
                instance_id=uuid.uuid4(), organization_id=org_id, external_id=external_id
            )
            session.add(conversation)
            await session.flush()
            conversation_id = conversation.id
        else:
            conversation_id = uuid.uuid4()  # card huérfana (sin conversación)
        session.add(
            Card(
                organization_id=org_id,
                conversation_id=conversation_id,
                stage_id=stage.id,
                title=title,
            )
        )
        await session.commit()


async def _board_of(
    session_factory: async_sessionmaker[AsyncSession], org_id: uuid.UUID
) -> BoardOut:
    async with session_factory() as session:
        return await BoardService(
            session=session,
            publisher=_NoopPublisher(),  # type: ignore[arg-type]
        ).get_board(org_id)


async def test_board_exposes_phone_from_conversation(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    org_id = uuid.uuid4()
    await _seed_card(session_factory, org_id, external_id="59171234567")

    cards = _cards(await _board_of(session_factory, org_id))

    assert len(cards) == 1
    assert cards[0].phone == "59171234567"


async def test_board_phone_is_tenant_scoped(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    org_a, org_b = uuid.uuid4(), uuid.uuid4()
    await _seed_card(session_factory, org_a, external_id="59170000001")
    await _seed_card(session_factory, org_b, external_id="59170000002")

    cards = _cards(await _board_of(session_factory, org_a))

    # Sólo la card de org_a, con su propio teléfono: sin fuga cross-tenant.
    assert [c.phone for c in cards] == ["59170000001"]


async def test_board_card_without_conversation_has_empty_phone(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    org_id = uuid.uuid4()
    await _seed_card(session_factory, org_id, external_id=None, title="Huérfana")

    cards = _cards(await _board_of(session_factory, org_id))

    # LEFT JOIN: la card aparece igual, con teléfono vacío (no desaparece del tablero).
    assert len(cards) == 1
    assert cards[0].phone == ""
