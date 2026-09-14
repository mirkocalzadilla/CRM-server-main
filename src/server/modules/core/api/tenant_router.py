import uuid
from typing import Annotated

from fastapi import APIRouter, Depends, Query, Response, status

from server.modules.core.api.deps import CurrentUser, DbSession, require_platform_operator
from server.modules.core.domain.schemas import (
    TenantRead,
    TenantUpdate,
    TenantUserCreate,
    TenantUserDetailRead,
    TenantUserRead,
    TenantUserUpdate,
)
from server.modules.core.services.membership_service import MembershipService
from server.modules.core.services.tenant_service import TenantService
from server.shared.exceptions import ForbiddenException

router = APIRouter(prefix="/tenants", tags=["tenants"])

# Config + ABM de users/roles del tenant: solo platform_operator (cross-tenant).
PlatformOperator = Annotated[object, Depends(require_platform_operator)]


@router.get("/me", response_model=TenantRead)
async def get_active_tenant(current: CurrentUser) -> TenantRead:
    return current.tenant


@router.patch("/me", response_model=TenantRead)
async def update_active_tenant(
    payload: TenantUpdate,
    session: DbSession,
    current: CurrentUser,
    _: PlatformOperator,
) -> TenantRead:
    service = TenantService(session)
    tenant = await service.update(current.tenant.id, payload)
    await session.commit()
    return TenantRead.model_validate(tenant)


@router.get("/me/members", response_model=list[TenantUserDetailRead])
async def list_members(
    session: DbSession,
    current: CurrentUser,
) -> list[TenantUserDetailRead]:
    members = await MembershipService(session).list_for_tenant(current.tenant.id)
    return [TenantUserDetailRead.model_validate(m) for m in members]


@router.post(
    "/me/members",
    status_code=status.HTTP_201_CREATED,
    response_model=TenantUserRead,
)
async def add_member(
    payload: TenantUserCreate,
    session: DbSession,
    current: CurrentUser,
    _: PlatformOperator,
) -> TenantUserRead:
    membership = await MembershipService(session).assign(
        tenant_id=current.tenant.id, user_id=payload.user_id, role=payload.role
    )
    await session.commit()
    return TenantUserRead.model_validate(membership)


@router.patch(
    "/me/members/{membership_id}",
    response_model=TenantUserRead,
)
async def update_member_role(
    membership_id: uuid.UUID,
    payload: TenantUserUpdate,
    session: DbSession,
    current: CurrentUser,
    _: PlatformOperator,
) -> TenantUserRead:
    service = MembershipService(session)
    target = await service.get(membership_id)
    if target.tenant_id != current.tenant.id:
        raise ForbiddenException("La membresía no pertenece al tenant activo")
    membership = await service.update_role(membership_id, payload.role)
    await session.commit()
    return TenantUserRead.model_validate(membership)


@router.delete(
    "/me/members/{membership_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    response_class=Response,
    response_model=None,
)
async def remove_member(
    membership_id: uuid.UUID,
    session: DbSession,
    current: CurrentUser,
    _: PlatformOperator,
) -> None:
    service = MembershipService(session)
    target = await service.get(membership_id)
    if target.tenant_id != current.tenant.id:
        raise ForbiddenException("La membresía no pertenece al tenant activo")
    await service.remove(membership_id)
    await session.commit()


@router.get("", response_model=list[TenantRead])
async def list_tenants(
    session: DbSession,
    _: PlatformOperator,
    limit: Annotated[int, Query(ge=1, le=200)] = 50,
    offset: Annotated[int, Query(ge=0)] = 0,
) -> list[TenantRead]:
    tenants = await TenantService(session).list(limit=limit, offset=offset)
    return [TenantRead.model_validate(t) for t in tenants]
