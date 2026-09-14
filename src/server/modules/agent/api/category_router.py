"""API de categorías del catálogo (#106). ABM org-scoped; exige `client_admin`
(o `platform_operator`, que pasa siempre por encima del rol por-tenant).

Las categorías son de la organización (no del agente). Borrar una deja sus
servicios sin categoría (no los elimina). `staff` → 403.
"""

from __future__ import annotations

import uuid
from typing import Annotated

from fastapi import APIRouter, Depends, Response, status

from server.modules.agent.api.catalog_schemas import (
    ServiceCategoryCreate,
    ServiceCategoryRead,
    ServiceCategoryUpdate,
)
from server.modules.agent.services.category_service import ServiceCategoryService
from server.modules.core.api.deps import DbSession, require_roles
from server.modules.core.domain.models import TenantUserRole
from server.modules.core.domain.schemas import AuthenticatedUser

router = APIRouter(tags=["agent-catalog"])

# Administrar categorías: client_admin + platform_operator; staff → 403.
CatalogManager = Annotated[AuthenticatedUser, Depends(require_roles(TenantUserRole.CLIENT_ADMIN))]


@router.get("/service-categories", response_model=list[ServiceCategoryRead])
async def list_categories(session: DbSession, ctx: CatalogManager) -> list[ServiceCategoryRead]:
    categories = await ServiceCategoryService(session).list_categories(ctx.tenant.id)
    return [ServiceCategoryRead.model_validate(category) for category in categories]


@router.post(
    "/service-categories",
    response_model=ServiceCategoryRead,
    status_code=status.HTTP_201_CREATED,
)
async def create_category(
    payload: ServiceCategoryCreate, session: DbSession, ctx: CatalogManager
) -> ServiceCategoryRead:
    category = await ServiceCategoryService(session).create_category(ctx.tenant.id, payload)
    return ServiceCategoryRead.model_validate(category)


@router.put("/service-categories/{category_id}", response_model=ServiceCategoryRead)
async def update_category(
    category_id: uuid.UUID, payload: ServiceCategoryUpdate, session: DbSession, ctx: CatalogManager
) -> ServiceCategoryRead:
    category = await ServiceCategoryService(session).update_category(
        category_id, ctx.tenant.id, payload
    )
    return ServiceCategoryRead.model_validate(category)


@router.delete(
    "/service-categories/{category_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    response_class=Response,
    response_model=None,
)
async def delete_category(
    category_id: uuid.UUID, session: DbSession, ctx: CatalogManager
) -> Response:
    await ServiceCategoryService(session).delete_category(category_id, ctx.tenant.id)
    return Response(status_code=status.HTTP_204_NO_CONTENT)
