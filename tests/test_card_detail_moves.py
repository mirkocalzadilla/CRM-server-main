"""Detalle de card: expone el historial `moves[]` (traceability) para el popup de
oportunidad (#75 / #55).

`get_card_detail` resuelve, por cada `card_move`, el nombre y el color (de
`stage_status`) de las stages origen/destino, ordenado asc por `moved_at`. El alta de
la card es el primer move sin origen (`stage_from_name=None`).
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from server.modules.agent.domain.models import Conversation
from server.modules.crm.api.schemas import CardDetailOut
from server.modules.crm.domain.models import Card, CardMove, Pipeline, Stage, StageStatus
from server.modules.crm.services.board_service import BoardService

OPEN_COLOR = "#3b82f6"
WON_COLOR = "#22c55e"
T0 = datetime(2026, 1, 1, 12, 0, 0, tzinfo=UTC)
T1 = datetime(2026, 1, 1, 12, 5, 0, tzinfo=UTC)


class _NoopPublisher:
    async def publish(self, channel: str, payload: dict[str, object]) -> None:
        return None


async def _detail_of(
    session_factory: async_sessionmaker[AsyncSession],
    card_id: uuid.UUID,
    org_id: uuid.UUID,
) -> CardDetailOut | None:
    async with session_factory() as session:
        return await BoardService(
            session=session,
            publisher=_NoopPublisher(),  # type: ignore[arg-type]
        ).get_card_detail(card_id, org_id)


async def _seed_card_with_moves(
    session_factory: async_sessionmaker[AsyncSession],
    org_id: uuid.UUID,
    *,
    with_moves: bool,
) -> uuid.UUID:
    """Siembra status + pipeline (2 stages) + conversación + card; opcionalmente 2 moves
    (alta sin origen → 'Nuevo'; luego 'Nuevo' → 'Ganado' por un humano). Devuelve card_id."""
    async with session_factory() as session:
        session.add(StageStatus(code="open", name="Abierto", color=OPEN_COLOR))
        session.add(StageStatus(code="won", name="Ganado", color=WON_COLOR))
        pipeline = Pipeline(organization_id=org_id, kind="ia", name="Gestión IA", position=0)
        session.add(pipeline)
        await session.flush()
        nuevo = Stage(pipeline_id=pipeline.id, name="Nuevo", position=0, status_code="open")
        ganado = Stage(pipeline_id=pipeline.id, name="Ganado", position=1, status_code="won")
        session.add_all([nuevo, ganado])
        await session.flush()
        conversation = Conversation(
            instance_id=uuid.uuid4(), organization_id=org_id, external_id="59171234567"
        )
        session.add(conversation)
        await session.flush()
        card = Card(
            organization_id=org_id,
            conversation_id=conversation.id,
            stage_id=ganado.id if with_moves else nuevo.id,
            title="Lead",
        )
        session.add(card)
        await session.flush()
        if with_moves:
            session.add_all(
                [
                    CardMove(
                        card_id=card.id,
                        stage_from_id=None,
                        stage_to_id=nuevo.id,
                        moved_by="agent",
                        moved_at=T0,
                    ),
                    CardMove(
                        card_id=card.id,
                        stage_from_id=nuevo.id,
                        stage_to_id=ganado.id,
                        moved_by="operator-123",
                        moved_at=T1,
                    ),
                ]
            )
        await session.commit()
        return card.id


async def test_card_detail_exposes_moves_chronologically_with_resolved_stages(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    org_id = uuid.uuid4()
    card_id = await _seed_card_with_moves(session_factory, org_id, with_moves=True)

    detail = await _detail_of(session_factory, card_id, org_id)

    assert detail is not None
    assert detail.phone == "59171234567"
    assert len(detail.moves) == 2

    # Alta: primer move sin origen, color resuelto desde stage_status.
    alta = detail.moves[0]
    assert alta.stage_from_name is None
    assert alta.stage_from_color is None
    assert alta.stage_to_name == "Nuevo"
    assert alta.stage_to_color == OPEN_COLOR
    assert alta.moved_by == "agent"

    # Segundo move (humano): origen y destino con nombre + color, orden por moved_at.
    cierre = detail.moves[1]
    assert cierre.stage_from_name == "Nuevo"
    assert cierre.stage_from_color == OPEN_COLOR
    assert cierre.stage_to_name == "Ganado"
    assert cierre.stage_to_color == WON_COLOR
    assert cierre.moved_by == "operator-123"


async def test_card_detail_without_moves_returns_empty_history(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    org_id = uuid.uuid4()
    card_id = await _seed_card_with_moves(session_factory, org_id, with_moves=False)

    detail = await _detail_of(session_factory, card_id, org_id)

    assert detail is not None
    assert detail.moves == []  # estado vacío, no error
