"""Move manual con motivo + audit log (server#253).

`move_card` acepta un `reason` opcional, lo persiste en `card_move` y emite una línea
`crm.card_moved` para auditoría. El historial (`get_card_detail`) devuelve el motivo
de cada move; las filas sin motivo quedan en `None`. La obligatoriedad para
Descalificado se impone en la UI: la API acepta el motivo siempre opcional.
"""

from __future__ import annotations

import uuid

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker
from structlog.testing import capture_logs

from server.modules.agent.domain.models import Conversation
from server.modules.crm.domain.models import Card, Pipeline, Stage, StageStatus
from server.modules.crm.services.board_service import BoardService


class _NoopPublisher:
    async def publish(self, channel: str, payload: dict[str, object]) -> None:
        return None


def _service(session: AsyncSession) -> BoardService:
    return BoardService(session=session, publisher=_NoopPublisher())  # type: ignore[arg-type]


async def _seed(
    session_factory: async_sessionmaker[AsyncSession], org_id: uuid.UUID
) -> tuple[uuid.UUID, uuid.UUID]:
    """Status + pipeline (origen → destino) + conversación + card en origen.
    Devuelve (card_id, target_stage_id)."""
    async with session_factory() as session:
        session.add(StageStatus(code="open", name="Abierto"))
        session.add(StageStatus(code="lost", name="Descalificado"))
        pipeline = Pipeline(organization_id=org_id, kind="ia", name="IA", position=0)
        session.add(pipeline)
        await session.flush()
        origin = Stage(pipeline_id=pipeline.id, name="Nuevo", position=0, status_code="open")
        target = Stage(
            pipeline_id=pipeline.id, name="Descalificado", position=1, status_code="lost"
        )
        session.add_all([origin, target])
        await session.flush()
        conversation = Conversation(
            instance_id=uuid.uuid4(), organization_id=org_id, external_id="59176389644"
        )
        session.add(conversation)
        await session.flush()
        card = Card(
            organization_id=org_id,
            conversation_id=conversation.id,
            stage_id=origin.id,
            title="Lead",
        )
        session.add(card)
        await session.commit()
        return card.id, target.id


async def _reason_in_history(
    session_factory: async_sessionmaker[AsyncSession], card_id: uuid.UUID, org_id: uuid.UUID
) -> str | None:
    async with session_factory() as session:
        detail = await _service(session).get_card_detail(card_id, org_id)
    assert detail is not None
    assert len(detail.moves) == 1  # el único move es el que acabamos de hacer
    return detail.moves[0].reason


async def test_move_with_reason_persists_and_history_reflects_it(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    org_id = uuid.uuid4()
    card_id, target_id = await _seed(session_factory, org_id)
    motivo = "Precio fuera de presupuesto"

    async with session_factory() as session:
        moved = await _service(session).move_card(card_id, target_id, uuid.uuid4(), org_id, motivo)
    assert moved is not None

    assert await _reason_in_history(session_factory, card_id, org_id) == motivo


async def test_move_without_reason_stores_null(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    org_id = uuid.uuid4()
    card_id, target_id = await _seed(session_factory, org_id)

    async with session_factory() as session:
        moved = await _service(session).move_card(card_id, target_id, uuid.uuid4(), org_id)
    assert moved is not None

    assert await _reason_in_history(session_factory, card_id, org_id) is None


async def test_move_emits_audit_log_with_reason(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    org_id = uuid.uuid4()
    card_id, target_id = await _seed(session_factory, org_id)
    moved_by = uuid.uuid4()
    motivo = "Lead duplicado"

    with capture_logs() as logs:
        async with session_factory() as session:
            await _service(session).move_card(card_id, target_id, moved_by, org_id, motivo)

    audit = [e for e in logs if e.get("event") == "crm.card_moved"]
    assert len(audit) == 1
    entry = audit[0]
    assert entry["reason"] == motivo
    assert entry["moved_by"] == str(moved_by)
    assert entry["stage_to"] == "Descalificado"
    assert entry["card_id"] == str(card_id)
