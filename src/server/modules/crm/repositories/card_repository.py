import uuid
from datetime import datetime
from typing import NamedTuple

from sqlalchemy import delete as sa_delete
from sqlalchemy import select
from sqlalchemy import update as sa_update
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import aliased

from server.modules.agent.domain.models import Conversation
from server.modules.crm.domain.models import Card, CardMove, CardService, Stage, StageStatus


class CardMoveRow(NamedTuple):
    """Fila del historial con nombre y color de las stages origen/destino resueltos.
    `stage_from_*` es None en el alta de la card (primer move sin origen)."""

    stage_from_name: str | None
    stage_from_color: str | None
    stage_to_name: str
    stage_to_color: str | None
    moved_by: str
    reason: str | None
    moved_at: datetime


class CardRepository:
    """Acceso a `card` + su historial `card_move` (1 card por conversation)."""

    def __init__(self, session: AsyncSession) -> None:
        self.session = session

    async def get_by_conversation(self, conversation_id: uuid.UUID) -> Card | None:
        result = await self.session.execute(
            select(Card).where(Card.conversation_id == conversation_id)
        )
        return result.scalar_one_or_none()

    async def get_stage_status(self, conversation_id: uuid.UUID) -> str | None:
        """`status_code` ('open'/'won'/'lost') del stage donde está la card de esta
        conversación, o None si no hay card. Usado para detectar leads cerrados."""
        result = await self.session.execute(
            select(Stage.status_code)
            .join(Card, Card.stage_id == Stage.id)
            .where(Card.conversation_id == conversation_id)
        )
        return result.scalar_one_or_none()

    async def add(self, card: Card) -> Card:
        self.session.add(card)
        await self.session.flush()
        await self.session.refresh(card)
        return card

    async def add_move(self, move: CardMove) -> None:
        self.session.add(move)
        await self.session.flush()

    async def link_to_contact_by_phone(
        self, organization_id: uuid.UUID, phone: str, contact_id: uuid.UUID
    ) -> None:
        """Link org cards whose conversation phone matches `phone` to `contact_id`.

        Tenant-scoped and idempotent (re-running is a no-op). Mirrors the migration
        backfill at runtime: the FK is resolved by `conversation.external_id == phone`
        within the same `organization_id`."""
        conv_ids = select(Conversation.id).where(
            Conversation.organization_id == organization_id,
            Conversation.external_id == phone,
        )
        await self.session.execute(
            sa_update(Card)
            .where(
                Card.organization_id == organization_id,
                Card.conversation_id.in_(conv_ids),
            )
            .values(contact_id=contact_id)
        )
        await self.session.flush()

    async def delete(self, card: Card) -> None:
        """Borrado duro: elimina el historial `card_move` y la card. La conversación
        queda (no se borra). En Postgres el FK ya cascadea; lo hacemos explícito para
        no dejar huérfanos donde el FK no se fuerza (SQLite de tests)."""
        await self.session.execute(sa_delete(CardMove).where(CardMove.card_id == card.id))
        await self.session.execute(sa_delete(CardService).where(CardService.card_id == card.id))
        await self.session.delete(card)
        await self.session.flush()

    async def list_moves(self, card_id: uuid.UUID) -> list[CardMoveRow]:
        """Historial de `card_move` de la card, asc por `moved_at`. Resuelve nombre y
        color (de `stage_status`) de las stages origen/destino con joins aliased.
        Origen va por outer join: el primer move (alta) no tiene `stage_from`."""
        stage_from = aliased(Stage)
        stage_to = aliased(Stage)
        status_from = aliased(StageStatus)
        status_to = aliased(StageStatus)
        result = await self.session.execute(
            select(
                stage_from.name.label("stage_from_name"),
                status_from.color.label("stage_from_color"),
                stage_to.name.label("stage_to_name"),
                status_to.color.label("stage_to_color"),
                CardMove.moved_by,
                CardMove.reason,
                CardMove.moved_at,
            )
            .outerjoin(stage_from, CardMove.stage_from_id == stage_from.id)
            .outerjoin(status_from, stage_from.status_code == status_from.code)
            .join(stage_to, CardMove.stage_to_id == stage_to.id)
            .outerjoin(status_to, stage_to.status_code == status_to.code)
            .where(CardMove.card_id == card_id)
            .order_by(CardMove.moved_at)
        )
        return [CardMoveRow(*row) for row in result.all()]
