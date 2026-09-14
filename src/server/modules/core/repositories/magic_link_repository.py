from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from server.modules.core.domain.models import MagicLinkToken


class MagicLinkRepository:
    def __init__(self, session: AsyncSession) -> None:
        self.session = session

    async def get_by_hash(self, token_hash: str) -> MagicLinkToken | None:
        result = await self.session.execute(
            select(MagicLinkToken).where(MagicLinkToken.token_hash == token_hash)
        )
        return result.scalar_one_or_none()

    async def add(self, token: MagicLinkToken) -> MagicLinkToken:
        self.session.add(token)
        await self.session.flush()
        await self.session.refresh(token)
        return token

    async def update(self, token: MagicLinkToken) -> MagicLinkToken:
        await self.session.flush()
        await self.session.refresh(token)
        return token
