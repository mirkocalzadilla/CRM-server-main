"""API de los links de entrega de un servicio (`service_link`).

Sub-recurso del ítem de catálogo (`/services/{service_id}/links`), mismo RBAC que el
resto del catálogo: `client_admin` + `platform_operator`; `staff` → 403. Los errores
los mapea el handler global (`NotFoundException` → 404, `ValidationException` → 422):
el router no captura nada, igual que el resto del catálogo.
"""

from __future__ import annotations

import uuid
from typing import Annotated

from fastapi import APIRouter, Depends, Response, status

from server.modules.agent.api.service_link_schemas import (
    ServiceLinkCreate,
    ServiceLinkRead,
    ServiceLinkUpdate,
)
from server.modules.agent.services.service_link_service import ServiceLinkService
from server.modules.core.api.deps import DbSession, require_roles
from server.modules.core.domain.models import TenantUserRole
from server.modules.core.domain.schemas import AuthenticatedUser

router = APIRouter(tags=["agent-catalog"])

CatalogManager = Annotated[AuthenticatedUser, Depends(require_roles(TenantUserRole.CLIENT_ADMIN))]


@router.get("/services/{service_id}/links", response_model=list[ServiceLinkRead])
async def list_links(
    service_id: uuid.UUID, session: DbSession, ctx: CatalogManager
) -> list[ServiceLinkRead]:
    links = await ServiceLinkService(session).list_links(service_id, ctx.tenant.id)
    return [ServiceLinkRead.model_validate(link) for link in links]


@router.post(
    "/services/{service_id}/links",
    response_model=ServiceLinkRead,
    status_code=status.HTTP_201_CREATED,
)
async def create_link(
    service_id: uuid.UUID,
    payload: ServiceLinkCreate,
    session: DbSession,
    ctx: CatalogManager,
) -> ServiceLinkRead:
    link = await ServiceLinkService(session).create_link(service_id, ctx.tenant.id, payload)
    return ServiceLinkRead.model_validate(link)


@router.put("/service-links/{link_id}", response_model=ServiceLinkRead)
async def update_link(
    link_id: uuid.UUID,
    payload: ServiceLinkUpdate,
    session: DbSession,
    ctx: CatalogManager,
) -> ServiceLinkRead:
    link = await ServiceLinkService(session).update_link(link_id, ctx.tenant.id, payload)
    return ServiceLinkRead.model_validate(link)


@router.delete(
    "/service-links/{link_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    response_class=Response,
    response_model=None,
)
async def delete_link(link_id: uuid.UUID, session: DbSession, ctx: CatalogManager) -> Response:
    await ServiceLinkService(session).delete_link(link_id, ctx.tenant.id)
    return Response(status_code=status.HTTP_204_NO_CONTENT)
