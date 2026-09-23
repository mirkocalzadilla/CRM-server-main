from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from server.modules.outbound.domain.models import (
    STATUS_DELIVERED,
    STATUS_READ,
    STATUS_SENT,
    OutboundMessage,
)

_COUNTED_AS_SENT = (STATUS_SENT, STATUS_DELIVERED, STATUS_READ)


class OutboundMessageRepository:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def add(self, message: OutboundMessage) -> OutboundMessage:
        self._session.add(message)
        await self._session.flush()
        return message

    async def get_by_wamid(self, wamid: str) -> OutboundMessage | None:
        result = await self._session.execute(
            select(OutboundMessage).where(OutboundMessage.wamid == wamid)
        )
        return result.scalar_one_or_none()

    async def exists_dedupe(self, dedupe_key: str) -> bool:
        result = await self._session.execute(
            select(OutboundMessage.id).where(OutboundMessage.dedupe_key == dedupe_key).limit(1)
        )
        return result.scalar_one_or_none() is not None

    async def count_sent_since(
        self, organization_id: uuid.UUID, purpose: str, since: datetime
    ) -> int:
        result = await self._session.execute(
            select(func.count())
            .select_from(OutboundMessage)
            .where(
                OutboundMessage.organization_id == organization_id,
                OutboundMessage.purpose == purpose,
                OutboundMessage.status.in_(_COUNTED_AS_SENT),
                OutboundMessage.created_at >= since,
            )
        )
        return int(result.scalar_one())

    async def last_sent_to(
        self, organization_id: uuid.UUID, wa_id: str, within: timedelta
    ) -> OutboundMessage | None:
        """Most recent message actually sent to this phone within `within`."""
        since = datetime.now(UTC) - within
        result = await self._session.execute(
            select(OutboundMessage)
            .where(
                OutboundMessage.organization_id == organization_id,
                OutboundMessage.wa_id == wa_id,
                OutboundMessage.status.in_(_COUNTED_AS_SENT),
                OutboundMessage.created_at >= since,
            )
            .order_by(OutboundMessage.created_at.desc())
            .limit(1)
        )
        return result.scalar_one_or_none()

    async def list_for_org(
        self,
        organization_id: uuid.UUID,
        *,
        purpose: str | None = None,
        status: str | None = None,
        limit: int = 100,
        offset: int = 0,
    ) -> list[OutboundMessage]:
        stmt = select(OutboundMessage).where(OutboundMessage.organization_id == organization_id)
        if purpose:
            stmt = stmt.where(OutboundMessage.purpose == purpose)
        if status:
            stmt = stmt.where(OutboundMessage.status == status)
        stmt = stmt.order_by(OutboundMessage.created_at.desc()).limit(limit).offset(offset)
        result = await self._session.execute(stmt)
        return list(result.scalars().all())

    async def list_for_card(
        self, card_id: uuid.UUID, organization_id: uuid.UUID
    ) -> list[OutboundMessage]:
        result = await self._session.execute(
            select(OutboundMessage)
            .where(
                OutboundMessage.card_id == card_id,
                OutboundMessage.organization_id == organization_id,
            )
            .order_by(OutboundMessage.created_at.desc())
        )
        return list(result.scalars().all())
