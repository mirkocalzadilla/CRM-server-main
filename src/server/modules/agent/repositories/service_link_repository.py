"""Acceso a `service_link` — links de entrega de un servicio, siempre org-scoped."""

from __future__ import annotations

import uuid
from collections.abc import Sequence

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from server.modules.agent.domain.catalog_models import ServiceLink


class ServiceLinkRepository:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def list_for_service(
        self, service_id: uuid.UUID, organization_id: uuid.UUID
    ) -> list[ServiceLink]:
        result = await self._session.execute(
            select(ServiceLink)
            .where(
                ServiceLink.service_id == service_id,
                ServiceLink.organization_id == organization_id,
            )
            .order_by(ServiceLink.orden, ServiceLink.created_at)
        )
        return list(result.scalars().all())

    async def list_for_services(
        self, service_ids: Sequence[uuid.UUID], organization_id: uuid.UUID
    ) -> list[ServiceLink]:
        """Links de varios servicios de una vez (el fulfillment resuelve la card entera)."""
        if not service_ids:
            return []
        result = await self._session.execute(
            select(ServiceLink)
            .where(
                ServiceLink.service_id.in_(service_ids),
                ServiceLink.organization_id == organization_id,
            )
            .order_by(ServiceLink.orden, ServiceLink.created_at)
        )
        return list(result.scalars().all())

    async def get(self, link_id: uuid.UUID, organization_id: uuid.UUID) -> ServiceLink | None:
        result = await self._session.execute(
            select(ServiceLink).where(
                ServiceLink.id == link_id,
                ServiceLink.organization_id == organization_id,
            )
        )
        return result.scalar_one_or_none()

    async def add(self, link: ServiceLink) -> ServiceLink:
        self._session.add(link)
        await self._session.flush()
        await self._session.refresh(link)
        return link

    async def delete(self, link: ServiceLink) -> None:
        await self._session.delete(link)
        await self._session.flush()
