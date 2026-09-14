import uuid

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from server.modules.core.domain.models import TenantUser


class TenantUserRepository:
    def __init__(self, session: AsyncSession) -> None:
        self.session = session

    async def get_by_id(self, membership_id: uuid.UUID) -> TenantUser | None:
        result = await self.session.execute(
            select(TenantUser).where(TenantUser.id == membership_id)
        )
        return result.scalar_one_or_none()

    async def get_by_tenant_and_user(
        self, tenant_id: uuid.UUID, user_id: uuid.UUID
    ) -> TenantUser | None:
        result = await self.session.execute(
            select(TenantUser).where(
                TenantUser.tenant_id == tenant_id,
                TenantUser.user_id == user_id,
            )
        )
        return result.scalar_one_or_none()

    async def list_by_user(self, user_id: uuid.UUID) -> list[TenantUser]:
        result = await self.session.execute(select(TenantUser).where(TenantUser.user_id == user_id))
        return list(result.scalars().all())

    async def list_by_tenant(self, tenant_id: uuid.UUID) -> list[TenantUser]:
        result = await self.session.execute(
            select(TenantUser).where(TenantUser.tenant_id == tenant_id)
        )
        return list(result.scalars().all())

    async def add(self, membership: TenantUser) -> TenantUser:
        self.session.add(membership)
        await self.session.flush()
        await self.session.refresh(membership)
        return membership

    async def update(self, membership: TenantUser) -> TenantUser:
        await self.session.flush()
        await self.session.refresh(membership)
        return membership

    async def delete(self, membership: TenantUser) -> None:
        await self.session.delete(membership)
        await self.session.flush()
