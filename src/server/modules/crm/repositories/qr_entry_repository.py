import uuid

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from server.modules.crm.domain.models import QrEntry


class QrEntryRepository:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def get_by_card(self, card_id: uuid.UUID) -> QrEntry | None:
        result = await self._session.execute(select(QrEntry).where(QrEntry.card_id == card_id))
        return result.scalar_one_or_none()

    async def add(self, entry: QrEntry) -> QrEntry:
        self._session.add(entry)
        await self._session.flush()
        await self._session.refresh(entry)
        return entry
