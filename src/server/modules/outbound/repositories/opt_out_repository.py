from __future__ import annotations

import uuid

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from server.modules.outbound.domain.models import MarketingOptOut


class OptOutRepository:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def is_opted_out(self, organization_id: uuid.UUID, wa_id: str) -> bool:
        result = await self._session.execute(
            select(MarketingOptOut.id)
            .where(
                MarketingOptOut.organization_id == organization_id,
                MarketingOptOut.wa_id == wa_id,
            )
            .limit(1)
        )
        return result.scalar_one_or_none() is not None

    async def add(self, organization_id: uuid.UUID, wa_id: str, source: str) -> bool:
        """Idempotent. Returns True when a new opt-out was recorded."""
        if await self.is_opted_out(organization_id, wa_id):
            return False
        self._session.add(
            MarketingOptOut(organization_id=organization_id, wa_id=wa_id, source=source)
        )
        await self._session.flush()
        return True

    async def list_for_org(self, organization_id: uuid.UUID) -> list[MarketingOptOut]:
        result = await self._session.execute(
            select(MarketingOptOut)
            .where(MarketingOptOut.organization_id == organization_id)
            .order_by(MarketingOptOut.created_at.desc())
        )
        return list(result.scalars().all())
