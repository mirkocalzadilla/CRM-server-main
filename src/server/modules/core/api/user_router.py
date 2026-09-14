import uuid
from typing import Annotated

from fastapi import APIRouter, Depends, Response, status

from server.modules.core.api.deps import CurrentUser, DbSession, require_user_manager
from server.modules.core.domain.models import TenantUser
from server.modules.core.domain.schemas import (
    AuthenticatedUser,
    TenantUserUpdate,
    UserCreateInTenant,
    UserRead,
    UserUpdate,
    UserWithRoleRead,
)
from server.modules.core.services.membership_service import MembershipService
from server.modules.core.services.user_service import UserService
from server.shared.exceptions import BadRequestException, ForbiddenException

router = APIRouter(prefix="/users", tags=["users"])

# ABM de usuarios/roles: platform_operator o client_admin (tenant-scoped, §RBAC #200).
UserManager = Annotated[AuthenticatedUser, Depends(require_user_manager)]


def _with_role(membership: TenantUser) -> UserWithRoleRead:
    return UserWithRoleRead(
        **UserRead.model_validate(membership.user).model_dump(),
        role=membership.role,
    )


@router.get("/me", response_model=UserRead)
async def get_me(current: CurrentUser) -> UserRead:
    return current.user


@router.patch("/me", response_model=UserRead)
async def update_me(
    payload: UserUpdate,
    session: DbSession,
    current: CurrentUser,
) -> UserRead:
    # Anti-self-lockout: no podés auto-desactivarte (§RBAC #200).
    if payload.is_active is False:
        raise BadRequestException("No puedes desactivar tu propia cuenta")
    user = await UserService(session).update(current.user.id, payload)
    await session.commit()
    return UserRead.model_validate(user)


@router.get("", response_model=list[UserWithRoleRead])
async def list_users(session: DbSession, ctx: UserManager) -> list[UserWithRoleRead]:
    """Usuarios del tenant activo con su rol de membresía."""
    memberships = await MembershipService(session).list_for_tenant(ctx.tenant.id)
    return [_with_role(m) for m in memberships]


@router.post("", response_model=UserWithRoleRead, status_code=status.HTTP_201_CREATED)
async def create_user(
    payload: UserCreateInTenant,
    session: DbSession,
    ctx: UserManager,
) -> UserWithRoleRead:
    """Alta de usuario en el tenant activo con rol asignable (email duplicado → 422).

    El rol asignable es solo ``client_admin``/``staff`` (el schema no expone
    ``is_superuser``): la creación de operadores queda script-only.
    """
    membership = await UserService(session).create_in_tenant(ctx.tenant.id, payload)
    await session.commit()
    return _with_role(membership)


@router.put("/{user_id}/role", response_model=UserWithRoleRead)
async def change_user_role(
    user_id: uuid.UUID,
    payload: TenantUserUpdate,
    session: DbSession,
    ctx: UserManager,
) -> UserWithRoleRead:
    """Cambia el rol del usuario en el tenant activo (client_admin ↔ staff)."""
    # Anti-self-lockout: no cambiar el rol propio (evita estados inválidos).
    if user_id == ctx.user.id:
        raise BadRequestException("No puedes cambiar tu propio rol")
    service = MembershipService(session)
    membership = await service.get_for_user_in_tenant(ctx.tenant.id, user_id)
    # Un client_admin no puede tocar a un platform_operator.
    if membership.user.is_superuser and not ctx.is_platform_operator:
        raise ForbiddenException("No puedes cambiar el rol de un platform_operator")
    updated = await service.update_role(membership.id, payload.role)
    await session.commit()
    return _with_role(updated)


@router.get("/{user_id}", response_model=UserRead)
async def get_user(
    user_id: uuid.UUID,
    session: DbSession,
    current: CurrentUser,
) -> UserRead:
    if user_id != current.user.id and not current.user.is_superuser:
        raise ForbiddenException("No puedes ver otros usuarios")
    user = await UserService(session).get(user_id)
    return UserRead.model_validate(user)


@router.delete(
    "/{user_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    response_class=Response,
    response_model=None,
)
async def delete_user(
    user_id: uuid.UUID,
    session: DbSession,
    ctx: UserManager,
) -> None:
    """Baja de usuario en el tenant activo (quita la membresía).

    Guardas: no auto-eliminarte, no borrar al último platform_operator, y un
    client_admin no puede eliminar a un platform_operator. El ``User`` global se
    borra solo si era su última membresía (evita impacto cross-tenant).
    """
    await UserService(session).remove_from_tenant(
        tenant_id=ctx.tenant.id,
        user_id=user_id,
        acting_user_id=ctx.user.id,
        actor_is_operator=ctx.is_platform_operator,
    )
    await session.commit()
