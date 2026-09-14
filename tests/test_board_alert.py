"""Read-model de alerta en la card (#94): la etiqueta se deriva del último
`handoff_event.reason` (sin campo nuevo ni migración). Sólo `unknown_service` (servicio
fuera del catálogo) se expone como alerta; el resto de los motivos no llevan etiqueta.
"""

from __future__ import annotations

import uuid

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from server.modules.agent.domain.models import Conversation, HandoffEvent
from server.modules.crm.api.schemas import BoardOut, CardDetailOut, CardOut
from server.modules.crm.domain.models import Card, Pipeline, Stage
from server.modules.crm.services.board_service import BoardService


class _NoopPublisher:
    async def publish(self, channel: str, payload: dict[str, object]) -> None:
        return None


def _cards(board: BoardOut) -> list[CardOut]:
    return [card for p in board.pipelines for s in p.stages for card in s.cards]


async def _seed_stage(
    session_factory: async_sessionmaker[AsyncSession], org_id: uuid.UUID
) -> uuid.UUID:
    """Siembra el pipeline Gestión Humana (único por org+kind) con su stage de intake."""
    async with session_factory() as session:
        pipeline = Pipeline(organization_id=org_id, kind="human", name="Gestión Humana", position=0)
        session.add(pipeline)
        await session.flush()
        stage = Stage(pipeline_id=pipeline.id, name="Por atender", position=0, status_code="open")
        session.add(stage)
        await session.commit()
        return stage.id


async def _seed_card(
    session_factory: async_sessionmaker[AsyncSession],
    org_id: uuid.UUID,
    stage_id: uuid.UUID,
    *,
    title: str,
    phone: str,
    handoff_reason: str | None,
) -> uuid.UUID:
    """Siembra conversación + card en el stage dado; opcionalmente un `handoff_event` con
    el motivo (thread_id = conversation.id). Devuelve el card_id."""
    async with session_factory() as session:
        # full_name = title: la proyección del board resuelve el título desde la
        # conversación (conversation.full_name → contact.full_name → phone).
        conversation = Conversation(
            instance_id=uuid.uuid4(), organization_id=org_id, external_id=phone, full_name=title
        )
        session.add(conversation)
        await session.flush()
        card = Card(
            organization_id=org_id,
            conversation_id=conversation.id,
            stage_id=stage_id,
            title=title,
        )
        session.add(card)
        await session.flush()
        if handoff_reason is not None:
            session.add(
                HandoffEvent(
                    agent_id=uuid.uuid4(),
                    organization_id=org_id,
                    thread_id=str(conversation.id),
                    reason=handoff_reason,
                )
            )
        await session.commit()
        return card.id


async def _board_of(
    session_factory: async_sessionmaker[AsyncSession], org_id: uuid.UUID
) -> BoardOut:
    async with session_factory() as session:
        return await BoardService(
            session=session,
            publisher=_NoopPublisher(),  # type: ignore[arg-type]
        ).get_board(org_id)


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


async def test_board_exposes_alert_only_for_unknown_service(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    org_id = uuid.uuid4()
    stage_id = await _seed_stage(session_factory, org_id)
    await _seed_card(
        session_factory,
        org_id,
        stage_id,
        title="Desconocido",
        phone="59170000001",
        handoff_reason="unknown_service",
    )
    await _seed_card(
        session_factory,
        org_id,
        stage_id,
        title="Pidió humano",
        phone="59170000002",
        handoff_reason="explicit_request",
    )
    await _seed_card(
        session_factory,
        org_id,
        stage_id,
        title="Sin handoff",
        phone="59170000003",
        handoff_reason=None,
    )

    alerts = {c.title: c.alert for c in _cards(await _board_of(session_factory, org_id))}

    assert alerts == {
        "Desconocido": "unknown_service",  # única con etiqueta de alerta
        "Pidió humano": None,  # otro motivo de handoff → sin alerta
        "Sin handoff": None,  # sin evento → sin alerta
    }


async def test_card_detail_exposes_unknown_service_alert(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    org_id = uuid.uuid4()
    stage_id = await _seed_stage(session_factory, org_id)
    card_id = await _seed_card(
        session_factory,
        org_id,
        stage_id,
        title="Desconocido",
        phone="59170000001",
        handoff_reason="unknown_service",
    )

    detail = await _detail_of(session_factory, card_id, org_id)

    assert detail is not None
    assert detail.alert == "unknown_service"
