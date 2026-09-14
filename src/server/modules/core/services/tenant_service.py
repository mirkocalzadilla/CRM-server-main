import uuid

from sqlalchemy.ext.asyncio import AsyncSession

from server.modules.core.domain.models import Tenant, TenantUser, TenantUserRole
from server.modules.core.domain.schemas import TenantCreate, TenantUpdate
from server.modules.core.repositories.tenant_repository import TenantRepository
from server.modules.core.repositories.tenant_user_repository import TenantUserRepository
from server.shared.exceptions import NotFoundException, ValidationException


class TenantService:
    def __init__(self, session: AsyncSession) -> None:
        self.session = session
        self.repo = TenantRepository(session)
        self.membership_repo = TenantUserRepository(session)

    async def create(self, payload: TenantCreate, owner_id: uuid.UUID | None = None) -> Tenant:
        existing = await self.repo.get_by_slug(payload.slug)
        if existing is not None:
            raise ValidationException(f"Ya existe un tenant con el slug '{payload.slug}'")

        tenant = Tenant(name=payload.name, slug=payload.slug.lower())
        tenant = await self.repo.add(tenant)

        if owner_id is not None:
            membership = TenantUser(
                tenant_id=tenant.id,
                user_id=owner_id,
                role=TenantUserRole.CLIENT_ADMIN,
            )
            await self.membership_repo.add(membership)

        return tenant

    async def get(self, tenant_id: uuid.UUID) -> Tenant:
        tenant = await self.repo.get_by_id(tenant_id)
        if tenant is None:
            raise NotFoundException(f"Tenant {tenant_id} no encontrado")
        return tenant

    async def get_by_slug(self, slug: str) -> Tenant:
        tenant = await self.repo.get_by_slug(slug.lower())
        if tenant is None:
            raise NotFoundException(f"Tenant '{slug}' no encontrado")
        return tenant

    async def list(self, limit: int = 50, offset: int = 0) -> list[Tenant]:
        return await self.repo.list_all(limit=limit, offset=offset)

    async def update(self, tenant_id: uuid.UUID, payload: TenantUpdate) -> Tenant:
        tenant = await self.get(tenant_id)
        if payload.name is not None:
            tenant.name = payload.name
        if payload.is_active is not None:
            tenant.is_active = payload.is_active
        return await self.repo.update(tenant)

    async def delete(self, tenant_id: uuid.UUID) -> None:
        tenant = await self.get(tenant_id)
        await self.repo.delete(tenant)
