import uuid

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from server.modules.core.domain.models import Tenant


class TenantRepository:
    def __init__(self, session: AsyncSession) -> None:
        self.session = session

    async def get_by_id(self, tenant_id: uuid.UUID) -> Tenant | None:
        result = await self.session.execute(select(Tenant).where(Tenant.id == tenant_id))
        return result.scalar_one_or_none()

    async def get_by_slug(self, slug: str) -> Tenant | None:
        result = await self.session.execute(select(Tenant).where(Tenant.slug == slug))
        return result.scalar_one_or_none()

    async def list_all(self, limit: int = 50, offset: int = 0) -> list[Tenant]:
        result = await self.session.execute(
            select(Tenant).order_by(Tenant.created_at.desc()).limit(limit).offset(offset)
        )
        return list(result.scalars().all())

    async def add(self, tenant: Tenant) -> Tenant:
        self.session.add(tenant)
        await self.session.flush()
        await self.session.refresh(tenant)
        return tenant

    async def update(self, tenant: Tenant) -> Tenant:
        await self.session.flush()
        await self.session.refresh(tenant)
        return tenant

    async def delete(self, tenant: Tenant) -> None:
        await self.session.delete(tenant)
        await self.session.flush()
