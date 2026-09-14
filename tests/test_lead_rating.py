"""Calificación (rating) hot/medium/cold + resumen IA en el read-model del CRM (#96).

El rating se deriva del `funnel_stage` (regla determinística, sin LLM) y se expone en
`CardOut.rating` (board) y `CardDetailOut.rating`; el resumen IA en `CardDetailOut.ai_summary`.
"""

from __future__ import annotations

import uuid

import pytest
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from server.modules.agent.domain.funnel_fsm import FunnelStage
from server.modules.agent.domain.models import Conversation
from server.modules.crm.api.schemas import BoardOut, CardOut
from server.modules.crm.domain.lead_rating import rating_for_stage
from server.modules.crm.domain.models import Card, Pipeline, Stage
from server.modules.crm.services.board_service import BoardService


@pytest.mark.parametrize(
    ("stage", "expected"),
    [
        (FunnelStage.QUALIFIED, "hot"),
        (FunnelStage.ENGAGING, "medium"),
        (FunnelStage.QUALIFYING, "medium"),
        (FunnelStage.HANDED_OFF, "medium"),
        (FunnelStage.NEW, "cold"),
        (FunnelStage.DISQUALIFIED, "cold"),
        (None, "cold"),
    ],
)
def test_rating_for_stage(stage: FunnelStage | None, expected: str) -> None:
    assert rating_for_stage(stage) == expected


@pytest.mark.parametrize(
    ("stage", "expected"),
    [
        # #206: aceptar un servicio calienta al lead en calificación/derivado (compra en
        # curso), aunque handed_off mapee a medium por defecto. Engaging sigue medium.
        (FunnelStage.HANDED_OFF, "hot"),
        (FunnelStage.QUALIFYING, "hot"),
        (FunnelStage.QUALIFIED, "hot"),
        (FunnelStage.ENGAGING, "medium"),
        (FunnelStage.NEW, "cold"),
        (FunnelStage.DISQUALIFIED, "cold"),
    ],
)
def test_rating_with_accepted_service(stage: FunnelStage, expected: str) -> None:
    assert rating_for_stage(stage, accepted_service=True) == expected


class _NoopPublisher:
    async def publish(self, channel: str, payload: dict[str, object]) -> None:
        return None


def _cards(board: BoardOut) -> list[CardOut]:
    return [card for p in board.pipelines for s in p.stages for card in s.cards]


async def _seed(
    session_factory: async_sessionmaker[AsyncSession],
    org_id: uuid.UUID,
    *,
    stage: FunnelStage,
    ai_summary: str | None = None,
) -> uuid.UUID:
    async with session_factory() as session:
        pipeline = Pipeline(organization_id=org_id, kind="ia", name="Gestión IA", position=0)
        session.add(pipeline)
        await session.flush()
        st = Stage(pipeline_id=pipeline.id, name="Nuevo", position=0, status_code="open")
        session.add(st)
        await session.flush()
        conv = Conversation(
            instance_id=uuid.uuid4(),
            organization_id=org_id,
            external_id="59171234567",
            funnel_stage=stage,
            ai_summary=ai_summary,
        )
        session.add(conv)
        await session.flush()
        card = Card(organization_id=org_id, conversation_id=conv.id, stage_id=st.id, title="Lead")
        session.add(card)
        await session.commit()
        return card.id


async def test_board_exposes_rating(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    org_id = uuid.uuid4()
    await _seed(session_factory, org_id, stage=FunnelStage.QUALIFIED)

    async with session_factory() as s:
        board = await BoardService(
            session=s,
            publisher=_NoopPublisher(),  # type: ignore[arg-type]
        ).get_board(org_id)

    cards = _cards(board)
    assert len(cards) == 1
    assert cards[0].rating == "hot"


async def test_card_detail_exposes_rating_and_summary(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    org_id = uuid.uuid4()
    card_id = await _seed(
        session_factory,
        org_id,
        stage=FunnelStage.QUALIFYING,
        ai_summary="Lead pregunta por edición; quiere el precio.",
    )

    async with session_factory() as s:
        detail = await BoardService(
            session=s,
            publisher=_NoopPublisher(),  # type: ignore[arg-type]
        ).get_card_detail(card_id, org_id)

    assert detail is not None
    assert detail.rating == "medium"
    assert detail.ai_summary == "Lead pregunta por edición; quiere el precio."
