"""Acceso a `service` — catálogo del agente, siempre org-scoped (multi-tenant).

Las lecturas de catálogo excluyen los servicios dados de baja (`deleted_at`); solo
`get_by_slug` los considera, para que el chequeo de unicidad respete la constraint
`(agent_id, slug)` aunque el slug pertenezca a un servicio eliminado.
"""

from __future__ import annotations

import uuid

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from server.modules.agent.domain.catalog_models import Service


class ServiceRepository:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def list_for_agent(
        self, agent_id: uuid.UUID, organization_id: uuid.UUID
    ) -> list[Service]:
        result = await self._session.execute(
            select(Service)
            .where(
                Service.agent_id == agent_id,
                Service.organization_id == organization_id,
                Service.deleted_at.is_(None),
            )
            .order_by(Service.orden, Service.nombre)
        )
        return list(result.scalars().unique().all())

    async def list_active_for_agent(
        self, agent_id: uuid.UUID, organization_id: uuid.UUID
    ) -> list[Service]:
        result = await self._session.execute(
            select(Service)
            .where(
                Service.agent_id == agent_id,
                Service.organization_id == organization_id,
                Service.is_active.is_(True),
                Service.deleted_at.is_(None),
            )
            .order_by(Service.orden, Service.nombre)
        )
        return list(result.scalars().unique().all())

    async def get(self, service_id: uuid.UUID, organization_id: uuid.UUID) -> Service | None:
        result = await self._session.execute(
            select(Service).where(
                Service.id == service_id,
                Service.organization_id == organization_id,
                Service.deleted_at.is_(None),
            )
        )
        return result.scalar_one_or_none()

    async def get_by_slug(self, agent_id: uuid.UUID, slug: str) -> Service | None:
        result = await self._session.execute(
            select(Service).where(Service.agent_id == agent_id, Service.slug == slug)
        )
        return result.scalar_one_or_none()

    async def add(self, service: Service) -> Service:
        self._session.add(service)
        await self._session.flush()
        await self._session.refresh(service)
        return service
