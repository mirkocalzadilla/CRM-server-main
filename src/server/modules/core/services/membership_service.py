import uuid

from sqlalchemy.ext.asyncio import AsyncSession

from server.modules.core.domain.models import TenantUser, TenantUserRole
from server.modules.core.repositories.tenant_user_repository import TenantUserRepository
from server.shared.exceptions import NotFoundException, ValidationException


class MembershipService:
    """Gestiona la relación entre usuarios y tenants (TenantUser)."""

    def __init__(self, session: AsyncSession) -> None:
        self.session = session
        self.repo = TenantUserRepository(session)

    async def assign(
        self,
        tenant_id: uuid.UUID,
        user_id: uuid.UUID,
        role: TenantUserRole = TenantUserRole.STAFF,
    ) -> TenantUser:
        existing = await self.repo.get_by_tenant_and_user(tenant_id, user_id)
        if existing is not None:
            raise ValidationException("El usuario ya pertenece a este tenant")

        membership = TenantUser(tenant_id=tenant_id, user_id=user_id, role=role)
        return await self.repo.add(membership)

    async def get(self, membership_id: uuid.UUID) -> TenantUser:
        membership = await self.repo.get_by_id(membership_id)
        if membership is None:
            raise NotFoundException(f"Membresía {membership_id} no encontrada")
        return membership

    async def get_for_user_in_tenant(self, tenant_id: uuid.UUID, user_id: uuid.UUID) -> TenantUser:
        membership = await self.repo.get_by_tenant_and_user(tenant_id, user_id)
        if membership is None:
            raise NotFoundException("El usuario no pertenece a este tenant")
        return membership

    async def list_for_user(self, user_id: uuid.UUID) -> list[TenantUser]:
        return await self.repo.list_by_user(user_id)

    async def list_for_tenant(self, tenant_id: uuid.UUID) -> list[TenantUser]:
        return await self.repo.list_by_tenant(tenant_id)

    async def update_role(self, membership_id: uuid.UUID, role: TenantUserRole) -> TenantUser:
        membership = await self.get(membership_id)
        membership.role = role
        return await self.repo.update(membership)

    async def remove(self, membership_id: uuid.UUID) -> None:
        membership = await self.get(membership_id)
        await self.repo.delete(membership)
