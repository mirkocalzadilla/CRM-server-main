"""Acceso a `service_category` — categorías del catálogo, org-scoped (#106)."""

from __future__ import annotations

import uuid

from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from server.modules.agent.domain.catalog_models import Service, ServiceCategory


class ServiceCategoryRepository:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def list_for_org(self, organization_id: uuid.UUID) -> list[ServiceCategory]:
        result = await self._session.execute(
            select(ServiceCategory)
            .where(ServiceCategory.organization_id == organization_id)
            .order_by(ServiceCategory.orden, ServiceCategory.nombre)
        )
        return list(result.scalars().all())

    async def get(
        self, category_id: uuid.UUID, organization_id: uuid.UUID
    ) -> ServiceCategory | None:
        result = await self._session.execute(
            select(ServiceCategory).where(
                ServiceCategory.id == category_id,
                ServiceCategory.organization_id == organization_id,
            )
        )
        return result.scalar_one_or_none()

    async def get_by_nombre(
        self, organization_id: uuid.UUID, nombre: str
    ) -> ServiceCategory | None:
        result = await self._session.execute(
            select(ServiceCategory).where(
                ServiceCategory.organization_id == organization_id,
                ServiceCategory.nombre == nombre,
            )
        )
        return result.scalar_one_or_none()

    async def get_by_slug(self, organization_id: uuid.UUID, slug: str) -> ServiceCategory | None:
        result = await self._session.execute(
            select(ServiceCategory).where(
                ServiceCategory.organization_id == organization_id,
                ServiceCategory.slug == slug,
            )
        )
        return result.scalar_one_or_none()

    async def add(self, category: ServiceCategory) -> ServiceCategory:
        self._session.add(category)
        await self._session.flush()
        await self._session.refresh(category)
        return category

    async def clear_from_services(self, category_id: uuid.UUID, organization_id: uuid.UUID) -> None:
        """Pone `category_id = NULL` en los servicios de esa categoría. Explícito (no
        depende del `ON DELETE SET NULL` del motor, que SQLite no aplica en tests)."""
        await self._session.execute(
            update(Service)
            .where(
                Service.category_id == category_id,
                Service.organization_id == organization_id,
            )
            .values(category_id=None)
            .execution_options(synchronize_session=False)
        )

    async def delete(self, category: ServiceCategory) -> None:
        await self._session.delete(category)
