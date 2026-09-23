from __future__ import annotations

import uuid
from datetime import datetime, timedelta
from typing import NamedTuple

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from server.modules.agent.domain.models import Conversation
from server.modules.crm.domain.event_models import EVENT_CLOSED, Event
from server.modules.crm.domain.models import Card, QrEntry


class ReminderTarget(NamedTuple):
    entry_id: uuid.UUID
    card_id: uuid.UUID
    conversation: Conversation


class ReminderRepository:
    """Upcoming events and the live entries that should be reminded."""

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def upcoming_events(self, now: datetime, horizon: timedelta) -> list[Event]:
        result = await self._session.execute(
            select(Event)
            .where(
                Event.status != EVENT_CLOSED,
                Event.starts_at > now,
                Event.starts_at <= now + horizon,
            )
            .order_by(Event.starts_at)
        )
        return list(result.scalars().unique().all())

    async def targets_for_event(self, event: Event) -> list[ReminderTarget]:
        """Entries of the event that are not revoked, with the lead's conversation."""
        result = await self._session.execute(
            select(QrEntry.id, Card.id, Conversation)
            .join(Card, Card.id == QrEntry.card_id)
            .join(Conversation, Conversation.id == Card.conversation_id)
            .where(
                QrEntry.event_id == event.id,
                QrEntry.revoked_at.is_(None),
                Card.organization_id == event.organization_id,
            )
        )
        return [
            ReminderTarget(entry_id=entry_id, card_id=card_id, conversation=conversation)
            for entry_id, card_id, conversation in result.unique().all()
        ]
