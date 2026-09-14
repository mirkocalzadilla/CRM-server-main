"""ABM de categorías del catálogo (#106) + sus materiales (#235). Org-scoped.

Borrar una categoría deja los servicios sin categoría (`category_id = NULL`, no los
elimina) y borra sus materiales (fila + archivo físico). Cada categoría lleva un
`slug` estable (derivado del nombre) que el agente usa en `enviar_material`. Todo
cambio re-sincroniza el snapshot que lee el agente en vivo.
"""

from __future__ import annotations

import re
import unicodedata
import uuid

import anyio
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from server.config import get_settings
from server.modules.agent.api.catalog_schemas import (
    ServiceCategoryCreate,
    ServiceCategoryUpdate,
)
from server.modules.agent.domain.catalog_models import ServiceCategory
from server.modules.agent.repositories.asset_repository import AssetRepository
from server.modules.agent.repositories.service_category_repository import (
    ServiceCategoryRepository,
)
from server.modules.agent.services.catalog_service import CatalogService
from server.shared.exceptions import NotFoundException, ValidationException


def _slugify(value: str) -> str:
    normalized = unicodedata.normalize("NFKD", value).encode("ascii", "ignore").decode("ascii")
    cleaned = re.sub(r"[^a-z0-9]+", "-", normalized.lower()).strip("-")
    return cleaned or "categoria"


class ServiceCategoryService:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session
        self._categories = ServiceCategoryRepository(session)
        self._assets = AssetRepository(session)

    async def list_categories(self, organization_id: uuid.UUID) -> list[ServiceCategory]:
        return await self._categories.list_for_org(organization_id)

    async def create_category(
        self, organization_id: uuid.UUID, payload: ServiceCategoryCreate
    ) -> ServiceCategory:
        if await self._categories.get_by_nombre(organization_id, payload.nombre) is not None:
            raise ValidationException(f"Ya existe una categoría '{payload.nombre}'")
        slug = await self._unique_slug(organization_id, payload.nombre)
        category = await self._categories.add(
            ServiceCategory(
                organization_id=organization_id,
                nombre=payload.nombre,
                orden=payload.orden,
                slug=slug,
            )
        )
        await self._set_materials(category, payload.asset_ids, organization_id)
        await self._session.commit()
        await CatalogService(self._session).sync_tenant_snapshot(organization_id)
        return await self._reload(category.id, organization_id)

    async def update_category(
        self,
        category_id: uuid.UUID,
        organization_id: uuid.UUID,
        payload: ServiceCategoryUpdate,
    ) -> ServiceCategory:
        category = await self._categories.get(category_id, organization_id)
        if category is None:
            raise NotFoundException(f"Categoría {category_id} no encontrada")
        changes = payload.model_dump(exclude_unset=True)
        asset_ids = changes.pop("asset_ids", None)
        new_nombre = changes.get("nombre")
        if new_nombre is not None:
            existing = await self._categories.get_by_nombre(organization_id, new_nombre)
            if existing is not None and existing.id != category.id:
                raise ValidationException(f"Ya existe una categoría '{new_nombre}'")
        for field, value in changes.items():
            setattr(category, field, value)  # slug queda estable (no se regenera)
        if asset_ids is not None:
            await self._set_materials(category, asset_ids, organization_id)
        await self._session.commit()
        await CatalogService(self._session).sync_tenant_snapshot(organization_id)
        return await self._reload(category.id, organization_id)

    async def delete_category(self, category_id: uuid.UUID, organization_id: uuid.UUID) -> None:
        """Borrado duro de la categoría: sus servicios quedan sin categoría y sus
        materiales se borran (fila + archivo físico)."""
        category = await self._categories.get(category_id, organization_id)
        if category is None:
            raise NotFoundException(f"Categoría {category_id} no encontrada")
        assets = await self._assets.list_for_category(category_id)
        storage_refs = [a.storage_ref for a in assets]
        # Explícito (no depende del ON DELETE CASCADE, que SQLite no aplica en tests).
        for asset in assets:
            await self._assets.delete(asset)
        await self._categories.clear_from_services(category_id, organization_id)
        await self._categories.delete(category)
        await self._session.commit()
        await CatalogService(self._session).sync_tenant_snapshot(organization_id)
        settings = get_settings()
        for storage_ref in storage_refs:
            await (anyio.Path(settings.media_root) / storage_ref).unlink(missing_ok=True)

    async def _unique_slug(self, organization_id: uuid.UUID, nombre: str) -> str:
        base = _slugify(nombre)
        slug = base
        suffix = 2
        while await self._categories.get_by_slug(organization_id, slug) is not None:
            slug = f"{base}-{suffix}"
            suffix += 1
        return slug

    async def _set_materials(
        self, category: ServiceCategory, asset_ids: list[uuid.UUID], organization_id: uuid.UUID
    ) -> None:
        """Enlaza los assets indicados a la categoría (≤5) y desvincula los que sobran.

        Valida que cada material exista en la organización; no borra los archivos
        desvinculados (la fila `asset` queda con `category_id = NULL`)."""
        resolved = []
        for asset_id in asset_ids:
            asset = await self._assets.get(asset_id, organization_id)
            if asset is None:
                raise ValidationException("El material referenciado no existe en esta organización")
            resolved.append(asset)
        keep = {asset.id for asset in resolved}
        for current in await self._assets.list_for_category(category.id):
            if current.id not in keep:
                current.category_id = None
        for asset in resolved:
            asset.category_id = category.id

    async def _reload(self, category_id: uuid.UUID, organization_id: uuid.UUID) -> ServiceCategory:
        """Re-lee la categoría refrescando `materials` tras (des)vincular assets."""
        result = await self._session.execute(
            select(ServiceCategory)
            .where(
                ServiceCategory.id == category_id,
                ServiceCategory.organization_id == organization_id,
            )
            .options(selectinload(ServiceCategory.materials))
            .execution_options(populate_existing=True)
        )
        category = result.scalar_one_or_none()
        if category is None:  # pragma: no cover - acabamos de escribirla
            raise NotFoundException(f"Categoría {category_id} no encontrada")
        return category
