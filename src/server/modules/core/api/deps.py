import uuid
from collections.abc import Callable, Coroutine
from typing import Annotated

import jwt
from fastapi import Cookie, Depends
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer, OAuth2PasswordBearer
from sqlalchemy.ext.asyncio import AsyncSession

from server.config import get_settings
from server.modules.core.domain.models import TenantUserRole, User
from server.modules.core.domain.schemas import AuthenticatedUser
from server.modules.core.services.tenant_service import TenantService
from server.modules.core.services.user_service import UserService
from server.shared.database import get_db_session
from server.shared.exceptions import (
    ForbiddenException,
    UnauthorizedException,
)

oauth2_scheme = OAuth2PasswordBearer(tokenUrl="/api/v1/auth/login", auto_error=False)
http_bearer = HTTPBearer(auto_error=False)


async def get_token(
    oauth2: Annotated[str | None, Depends(oauth2_scheme)] = None,
    bearer: Annotated[HTTPAuthorizationCredentials | None, Depends(http_bearer)] = None,
    cookie_token: Annotated[str | None, Cookie(alias="access_token")] = None,
) -> str | None:
    if oauth2:
        return oauth2
    if bearer:
        return bearer.credentials
    if cookie_token:
        return cookie_token
    return None


DbSession = Annotated[AsyncSession, Depends(get_db_session)]


async def get_current_context(
    session: DbSession,
    token: Annotated[str | None, Depends(get_token)],
) -> AuthenticatedUser:
    """Extrae usuario y tenant activo desde el JWT (sub, tenant_id, role)."""
    if token is None:
        raise UnauthorizedException("Token no provisto")

    settings = get_settings()
    try:
        payload = jwt.decode(token, settings.jwt_secret_key, algorithms=[settings.jwt_algorithm])
    except jwt.InvalidTokenError as exc:
        raise UnauthorizedException("Token inválido") from exc

    sub = payload.get("sub")
    tenant_id_raw = payload.get("tenant_id")
    role_raw = payload.get("role")
    if sub is None or tenant_id_raw is None or role_raw is None:
        raise UnauthorizedException("Token malformado")

    try:
        user_id = uuid.UUID(str(sub))
        tenant_id = uuid.UUID(str(tenant_id_raw))
        role = TenantUserRole(str(role_raw))
    except (ValueError, KeyError) as exc:
        raise UnauthorizedException("Token malformado") from exc

    user = await UserService(session).get(user_id)
    if not user.is_active:
        raise UnauthorizedException("Usuario inactivo")

    tenant = await TenantService(session).get(tenant_id)
    if not tenant.is_active:
        raise UnauthorizedException("Tenant inactivo")

    # is_platform_operator se deriva del usuario en DB (autoritativo): si se revoca
    # is_superuser, deja de operar al instante sin esperar a que expire el token.
    return AuthenticatedUser.model_validate(
        {
            "user": user,
            "tenant": tenant,
            "role": role,
            "is_platform_operator": user.is_superuser,
        }
    )


CurrentUser = Annotated[AuthenticatedUser, Depends(get_current_context)]


def require_roles(
    *allowed: TenantUserRole,
) -> "Callable[..., Coroutine[None, None, AuthenticatedUser]]":
    """Factory de dependencias que exige uno de los roles por-tenant dados.

    El ``platform_operator`` (is_superuser) pasa siempre: opera cross-tenant por
    encima del rol por-tenant.
    """

    async def _checker(ctx: CurrentUser) -> AuthenticatedUser:
        if ctx.is_platform_operator:
            return ctx
        if ctx.role not in allowed:
            raise ForbiddenException("Permisos insuficientes para esta operación")
        return ctx

    return _checker


async def require_platform_operator(ctx: CurrentUser) -> AuthenticatedUser:
    """Exige rol global ``platform_operator`` (``User.is_superuser``).

    Gatea config/agente: 403 para client_admin/staff.
    """
    if not ctx.is_platform_operator:
        raise ForbiddenException("Requiere rol platform_operator")
    return ctx


async def require_user_manager(ctx: CurrentUser) -> AuthenticatedUser:
    """Exige capacidad de *gestionar usuarios*: ``platform_operator`` o ``client_admin``.

    Separa "gestionar usuarios" de "config del agente" (que sigue en
    ``require_platform_operator``): el ``client_admin`` administra su propio staff
    dentro de su tenant. El enforcement tenant-scoped (no tocar operadores, solo su
    tenant) vive en los endpoints/servicio (#200, §RBAC).
    """
    if ctx.is_platform_operator or ctx.role == TenantUserRole.CLIENT_ADMIN:
        return ctx
    raise ForbiddenException("Requiere rol client_admin o platform_operator")


def get_user_from_request(user: User) -> User:
    """Helper auxiliar para tests futuros — placeholder explícito."""
    return user
