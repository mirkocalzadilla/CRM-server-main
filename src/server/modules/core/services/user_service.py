import uuid

from sqlalchemy.ext.asyncio import AsyncSession

from server.modules.core.domain.models import TenantUser, User
from server.modules.core.domain.schemas import UserCreate, UserCreateInTenant, UserUpdate
from server.modules.core.repositories.tenant_user_repository import TenantUserRepository
from server.modules.core.repositories.user_repository import UserRepository
from server.shared.exceptions import (
    BadRequestException,
    ForbiddenException,
    NotFoundException,
    ValidationException,
)
from server.shared.security import get_password_hash


class UserService:
    def __init__(self, session: AsyncSession) -> None:
        self.session = session
        self.repo = UserRepository(session)
        self.membership_repo = TenantUserRepository(session)

    async def create(self, payload: UserCreate) -> User:
        existing = await self.repo.get_by_email(payload.email)
        if existing is not None:
            raise ValidationException(f"Ya existe un usuario con el email {payload.email}")

        user = User(
            email=payload.email.lower(),
            full_name=payload.full_name,
            hashed_password=get_password_hash(payload.password),
        )
        return await self.repo.add(user)

    async def create_in_tenant(
        self, tenant_id: uuid.UUID, payload: UserCreateInTenant
    ) -> TenantUser:
        """Crea un usuario y lo asigna como miembro del tenant con el rol dado."""
        user = await self.create(
            UserCreate(
                email=payload.email,
                password=payload.password,
                full_name=payload.full_name,
            )
        )
        membership = TenantUser(tenant_id=tenant_id, user_id=user.id, role=payload.role)
        return await self.membership_repo.add(membership)

    async def get(self, user_id: uuid.UUID) -> User:
        user = await self.repo.get_by_id(user_id)
        if user is None:
            raise NotFoundException(f"Usuario {user_id} no encontrado")
        return user

    async def get_by_email(self, email: str) -> User | None:
        return await self.repo.get_by_email(email)

    async def list(self, limit: int = 50, offset: int = 0) -> list[User]:
        return await self.repo.list_all(limit=limit, offset=offset)

    async def update(self, user_id: uuid.UUID, payload: UserUpdate) -> User:
        user = await self.get(user_id)
        if payload.full_name is not None:
            user.full_name = payload.full_name
        if payload.is_active is not None:
            user.is_active = payload.is_active
        return await self.repo.update(user)

    async def set_password(self, user: User, new_password: str) -> User:
        """Rehashea y persiste una nueva contraseña sobre un usuario ya cargado."""
        user.hashed_password = get_password_hash(new_password)
        return await self.repo.update(user)

    async def remove_from_tenant(
        self,
        *,
        tenant_id: uuid.UUID,
        user_id: uuid.UUID,
        acting_user_id: uuid.UUID,
        actor_is_operator: bool,
    ) -> None:
        """Quita la membresía del usuario en el tenant activo (§RBAC #200).

        Guardas, en orden: no auto-baja, un client_admin no toca operadores, y no
        eliminar al último platform_operator. El ``User`` global se borra solo si
        era su última membresía (evita impacto cross-tenant).
        """
        if user_id == acting_user_id:
            raise BadRequestException("No puedes eliminar tu propia cuenta")
        user = await self.get(user_id)
        # Un client_admin no puede eliminar a un platform_operator.
        if user.is_superuser and not actor_is_operator:
            raise ForbiddenException("No puedes eliminar a un platform_operator")
        # Invariante: nunca dejar el sistema sin platform_operator.
        if user.is_superuser and await self.repo.count_superusers() <= 1:
            raise BadRequestException("No puedes eliminar al último platform_operator")
        memberships = await self.membership_repo.list_by_user(user_id)
        target = next((m for m in memberships if m.tenant_id == tenant_id), None)
        if target is None:
            raise NotFoundException("El usuario no pertenece a este tenant")
        if len(memberships) > 1:
            # Multi-tenant: quita solo esta membresía; el User global sobrevive.
            await self.membership_repo.delete(target)
        else:
            # Última membresía: borra el User global (la FK cascada limpia la membresía).
            await self.repo.delete(user)
