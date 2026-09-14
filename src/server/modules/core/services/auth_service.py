import uuid
from datetime import timedelta

from sqlalchemy.ext.asyncio import AsyncSession

from server.config import get_settings
from server.modules.core.domain.models import (
    Tenant,
    TenantUser,
    TenantUserRole,
    User,
)
from server.modules.core.domain.schemas import (
    LoginRequest,
    RegisterRequest,
    TokenResponse,
)
from server.modules.core.services.tenant_service import TenantService
from server.modules.core.services.user_service import UserService
from server.shared.exceptions import (
    BadRequestException,
    UnauthorizedException,
    ValidationException,
)
from server.shared.security import create_access_token, verify_password


class AuthService:
    """Maneja registro y login. Devuelve JWTs con (sub, tenant_id, role)."""

    def __init__(self, session: AsyncSession) -> None:
        self.session = session
        self.user_service = UserService(session)
        self.tenant_service = TenantService(session)

    async def register(
        self, payload: RegisterRequest
    ) -> tuple[User, Tenant, TenantUser, TokenResponse]:
        if await self.user_service.get_by_email(payload.email) is not None:
            raise ValidationException(f"El email {payload.email} ya está registrado")

        from server.modules.core.domain.schemas import TenantCreate, UserCreate

        user = await self.user_service.create(
            UserCreate(
                email=payload.email,
                password=payload.password,
                full_name=payload.full_name,
            )
        )
        tenant = await self.tenant_service.create(
            TenantCreate(name=payload.tenant_name, slug=payload.tenant_slug),
            owner_id=user.id,
        )
        membership = await self.tenant_service.membership_repo.get_by_tenant_and_user(
            tenant.id, user.id
        )
        assert membership is not None

        token = self._issue_token(user, tenant, membership.role)
        return user, tenant, membership, token

    async def login(self, payload: LoginRequest) -> tuple[User, Tenant, TenantUser, TokenResponse]:
        user = await self.user_service.get_by_email(payload.email)
        if user is None or user.hashed_password is None:
            raise UnauthorizedException("Credenciales inválidas")
        if not verify_password(payload.password, user.hashed_password):
            raise UnauthorizedException("Credenciales inválidas")
        if not user.is_active:
            raise UnauthorizedException("Usuario inactivo")

        memberships = user.memberships
        if not memberships:
            raise UnauthorizedException("El usuario no pertenece a ningún tenant")

        primary = memberships[0]
        tenant = await self.tenant_service.get(primary.tenant_id)
        token = self._issue_token(user, tenant, primary.role)
        return user, tenant, primary, token

    async def change_password(
        self, user_id: uuid.UUID, current_password: str, new_password: str
    ) -> None:
        """Cambia la contraseña del usuario probando la actual. 400 si no coincide."""
        user = await self.user_service.get(user_id)
        if user.hashed_password is None or not verify_password(
            current_password, user.hashed_password
        ):
            raise BadRequestException("La contraseña actual es incorrecta")
        await self.user_service.set_password(user, new_password)

    def _issue_token(self, user: User, tenant: Tenant, role: TenantUserRole) -> TokenResponse:
        settings = get_settings()
        expires = timedelta(minutes=settings.jwt_access_token_expire_minutes)
        access_token = create_access_token(
            data={
                "sub": str(user.id),
                "tenant_id": str(tenant.id),
                "role": role.value,
                "is_superuser": user.is_superuser,
            },
            expires_delta=expires,
        )
        return TokenResponse(
            access_token=access_token,
            token_type="bearer",
            expires_in=int(expires.total_seconds()),
        )
