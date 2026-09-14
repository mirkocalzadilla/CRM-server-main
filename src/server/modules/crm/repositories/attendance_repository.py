"""Lista de asistencia de un evento: quién tiene entrada y quién ya entró."""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import NamedTuple

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from server.modules.agent.domain.models import Conversation
from server.modules.crm.domain.models import Card, QrEntry


class AttendeeRow(NamedTuple):
    entry_id: uuid.UUID
    card_id: uuid.UUID
    lead_name: str
    used_at: datetime | None
    used_manually: bool
    revoked_at: datetime | None


class AttendanceRepository:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def for_event(self, event_id: uuid.UUID, organization_id: uuid.UUID) -> list[AttendeeRow]:
        """Entradas del evento, ordenadas por nombre para buscar a alguien en la lista.

        Incluye las revocadas: quien atiende necesita poder decirle a alguien que su
        entrada fue anulada, no que "no aparece".
        """
        result = await self._session.execute(
            select(
                QrEntry.id,
                Card.id,
                Conversation.full_name,
                Card.title,
                QrEntry.used_at,
                QrEntry.used_manually,
                QrEntry.revoked_at,
            )
            .join(Card, Card.id == QrEntry.card_id)
            .outerjoin(Conversation, Conversation.id == Card.conversation_id)
            .where(QrEntry.event_id == event_id, Card.organization_id == organization_id)
            .order_by(Conversation.full_name, Card.title)
        )
        return [
            AttendeeRow(
                entry_id=entry_id,
                card_id=card_id,
                # El nombre de la conversación, y si no el título de la card: en la
                # puerta se busca por nombre, así que nunca se muestra un id.
                lead_name=full_name or title,
                used_at=used_at,
                used_manually=used_manually,
                revoked_at=revoked_at,
            )
            for entry_id, card_id, full_name, title, used_at, used_manually, revoked_at in (
                result.tuples().all()
            )
        ]
