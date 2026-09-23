"""API de M-Outbound (`/outbound`): historial de envíos, configuración de la reactivación,
bajas y envíos manuales desde una card.

RBAC: el historial, las bajas y los envíos manuales son operación diaria (los 3 roles);
la configuración de la reactivación es del negocio (`client_admin` + operador), como
la de pagos.
"""

from __future__ import annotations

import uuid
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Query, status

from server.modules.core.api.deps import CurrentUser, DbSession, require_roles
from server.modules.core.domain.models import TenantUserRole
from server.modules.core.domain.schemas import AuthenticatedUser
from server.modules.outbound.api.schemas import (
    ManualSendResult,
    OptOutCreate,
    OptOutRead,
    OutboundMessagePage,
    OutboundMessageRead,
    OutboundSettingsRead,
    OutboundSettingsUpdate,
)
from server.modules.outbound.services.admin_service import OutboundAdminService
from server.shared.exceptions import NotFoundException, ValidationException

router = APIRouter(prefix="/outbound", tags=["outbound"])

ConfigManager = Annotated[AuthenticatedUser, Depends(require_roles(TenantUserRole.CLIENT_ADMIN))]


@router.get("/messages", response_model=OutboundMessagePage)
async def list_messages(
    ctx: CurrentUser,
    session: DbSession,
    purpose: str | None = None,
    status_filter: Annotated[str | None, Query(alias="status")] = None,
    limit: Annotated[int, Query(ge=1, le=200)] = 50,
    offset: Annotated[int, Query(ge=0)] = 0,
) -> OutboundMessagePage:
    rows = await OutboundAdminService(session).list_messages(
        ctx.tenant.id, purpose=purpose, status=status_filter, limit=limit, offset=offset
    )
    return OutboundMessagePage(
        items=[OutboundMessageRead.model_validate(r) for r in rows], limit=limit, offset=offset
    )


@router.get("/cards/{card_id}/messages", response_model=list[OutboundMessageRead])
async def list_card_messages(
    card_id: uuid.UUID, ctx: CurrentUser, session: DbSession
) -> list[OutboundMessageRead]:
    rows = await OutboundAdminService(session).list_for_card(card_id, ctx.tenant.id)
    return [OutboundMessageRead.model_validate(r) for r in rows]


@router.get("/settings", response_model=OutboundSettingsRead)
async def read_settings(ctx: ConfigManager, session: DbSession) -> OutboundSettingsRead:
    return OutboundSettingsRead.model_validate(
        await OutboundAdminService(session).read_settings(ctx.tenant.id)
    )


@router.put("/settings", response_model=OutboundSettingsRead)
async def update_settings(
    payload: OutboundSettingsUpdate, ctx: ConfigManager, session: DbSession
) -> OutboundSettingsRead:
    try:
        current = await OutboundAdminService(session).update_settings(ctx.tenant.id, payload)
    except ValidationException as exc:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=exc.message) from exc
    return OutboundSettingsRead.model_validate(current)


@router.get("/opt-outs", response_model=list[OptOutRead])
async def list_opt_outs(ctx: CurrentUser, session: DbSession) -> list[OptOutRead]:
    rows = await OutboundAdminService(session).list_opt_outs(ctx.tenant.id)
    return [OptOutRead.model_validate(r) for r in rows]


@router.post("/opt-outs", status_code=status.HTTP_201_CREATED)
async def add_opt_out(
    payload: OptOutCreate, ctx: CurrentUser, session: DbSession
) -> dict[str, str]:
    await OutboundAdminService(session).add_opt_out(ctx.tenant.id, payload.wa_id)
    return {"status": "ok"}


@router.post("/cards/{card_id}/remind", response_model=ManualSendResult)
async def remind_card(card_id: uuid.UUID, ctx: CurrentUser, session: DbSession) -> ManualSendResult:
    try:
        return await OutboundAdminService(session).remind_card(card_id, ctx.tenant.id)
    except NotFoundException as exc:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=exc.message) from exc


@router.post("/cards/{card_id}/reactivate", response_model=ManualSendResult)
async def reactivate_card(
    card_id: uuid.UUID, ctx: CurrentUser, session: DbSession
) -> ManualSendResult:
    try:
        return await OutboundAdminService(session).reactivate_card(card_id, ctx.tenant.id)
    except NotFoundException as exc:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=exc.message) from exc
