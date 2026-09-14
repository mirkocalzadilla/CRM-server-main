"""API del catálogo (SPEC_admin_catalogo_kb §5). Exige rol `client_admin` (o
`platform_operator`, que pasa siempre por estar por encima del rol por-tenant).

El catálogo es **del tenant**: los endpoints no exponen el agente. El backend
resuelve el agente del tenant por detrás (MVP = uno por tenant) y re-proyecta los
servicios activos al snapshot que lee el agente en vivo (#109: sin botón de
"publicar"). `staff` → 403 (RBAC 3 niveles, core/api/deps.py). La config del agente
sigue siendo platform_operator-only (config_router); el client_admin no la toca.
"""

from __future__ import annotations

import uuid
from typing import Annotated

from fastapi import APIRouter, Depends, File, Response, UploadFile, status

from server.modules.agent.api.catalog_schemas import (
    AssetRead,
    ServiceCreate,
    ServiceRead,
    ServiceUpdate,
)
from server.modules.agent.services.asset_service import MATERIAL_MAX_BYTES, AssetService
from server.modules.agent.services.catalog_service import CatalogService
from server.modules.core.api.deps import DbSession, require_roles
from server.modules.core.domain.models import TenantUserRole
from server.modules.core.domain.schemas import AuthenticatedUser
from server.shared.exceptions import ValidationException

router = APIRouter(tags=["agent-catalog"])

# Administrar el catálogo: client_admin + platform_operator; staff → 403.
CatalogManager = Annotated[AuthenticatedUser, Depends(require_roles(TenantUserRole.CLIENT_ADMIN))]


@router.get("/catalog/services", response_model=list[ServiceRead])
async def list_services(session: DbSession, ctx: CatalogManager) -> list[ServiceRead]:
    services = await CatalogService(session).list_services_for_tenant(ctx.tenant.id)
    return [ServiceRead.model_validate(service) for service in services]


@router.post("/catalog/services", response_model=ServiceRead, status_code=status.HTTP_201_CREATED)
async def create_service(
    payload: ServiceCreate, session: DbSession, ctx: CatalogManager
) -> ServiceRead:
    service = await CatalogService(session).create_service_for_tenant(ctx.tenant.id, payload)
    return ServiceRead.model_validate(service)


@router.put("/services/{service_id}", response_model=ServiceRead)
async def update_service(
    service_id: uuid.UUID, payload: ServiceUpdate, session: DbSession, ctx: CatalogManager
) -> ServiceRead:
    service = await CatalogService(session).update_service(service_id, ctx.tenant.id, payload)
    return ServiceRead.model_validate(service)


@router.delete(
    "/services/{service_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    response_class=Response,
    response_model=None,
)
async def delete_service(
    service_id: uuid.UUID, session: DbSession, ctx: CatalogManager
) -> Response:
    await CatalogService(session).delete_service(service_id, ctx.tenant.id)
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.post("/assets", response_model=AssetRead, status_code=status.HTTP_201_CREATED)
async def upload_asset(
    session: DbSession, ctx: CatalogManager, file: Annotated[UploadFile, File()]
) -> AssetRead:
    if file.size is not None and file.size > MATERIAL_MAX_BYTES:
        raise ValidationException("El archivo supera el límite de 5 MB")
    data = await file.read()
    asset = await AssetService(session).upload_material(
        organization_id=ctx.tenant.id,
        content_type=file.content_type,
        filename=file.filename,
        data=data,
    )
    return AssetRead.model_validate(asset)


@router.delete(
    "/assets/{asset_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    response_class=Response,
    response_model=None,
)
async def delete_asset(asset_id: uuid.UUID, session: DbSession, ctx: CatalogManager) -> Response:
    await AssetService(session).delete_material(asset_id, ctx.tenant.id)
    return Response(status_code=status.HTTP_204_NO_CONTENT)
