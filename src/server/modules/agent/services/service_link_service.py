"""ABM de los links de entrega de un servicio (`service_link`).

Los links NO se proyectan al snapshot del agente (ver `catalog_snapshot_service`):
son material de entrega post-pago, no de venta, así que no hace falta re-sincronizar
nada al tocarlos. Todo org-scoped; el servicio se valida antes de escribir.
"""

from __future__ import annotations

import uuid

from sqlalchemy.ext.asyncio import AsyncSession

from server.modules.agent.api.service_link_schemas import ServiceLinkCreate, ServiceLinkUpdate
from server.modules.agent.domain.catalog_models import ServiceLink
from server.modules.agent.repositories.service_link_repository import ServiceLinkRepository
from server.modules.agent.repositories.service_repository import ServiceRepository
from server.shared.exceptions import NotFoundException, ValidationException

# Tope por servicio: la entrega es un mensaje de WhatsApp, no un directorio de links.
MAX_LINKS_PER_SERVICE = 5


class ServiceLinkService:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session
        self._links = ServiceLinkRepository(session)
        self._services = ServiceRepository(session)

    async def _require_service_id(
        self, service_id: uuid.UUID, organization_id: uuid.UUID
    ) -> uuid.UUID:
        service = await self._services.get(service_id, organization_id)
        if service is None:
            raise NotFoundException(f"Servicio {service_id} no encontrado")
        return service.id

    async def list_links(
        self, service_id: uuid.UUID, organization_id: uuid.UUID
    ) -> list[ServiceLink]:
        await self._require_service_id(service_id, organization_id)
        return await self._links.list_for_service(service_id, organization_id)

    async def create_link(
        self, service_id: uuid.UUID, organization_id: uuid.UUID, payload: ServiceLinkCreate
    ) -> ServiceLink:
        await self._require_service_id(service_id, organization_id)
        existing = await self._links.list_for_service(service_id, organization_id)
        if len(existing) >= MAX_LINKS_PER_SERVICE:
            raise ValidationException(f"Máximo {MAX_LINKS_PER_SERVICE} links por servicio")
        link = await self._links.add(
            ServiceLink(
                organization_id=organization_id,
                service_id=service_id,
                **payload.model_dump(),
            )
        )
        await self._session.commit()
        return link

    async def update_link(
        self, link_id: uuid.UUID, organization_id: uuid.UUID, payload: ServiceLinkUpdate
    ) -> ServiceLink:
        link = await self._links.get(link_id, organization_id)
        if link is None:
            raise NotFoundException(f"Link {link_id} no encontrado")
        for field, value in payload.model_dump(exclude_unset=True).items():
            setattr(link, field, value)
        await self._session.commit()
        # `updated_at` lo pone la DB (`onupdate`): sin refresh queda expirado y el
        # schema lo leería con IO lazy fuera del contexto async.
        await self._session.refresh(link)
        return link

    async def delete_link(self, link_id: uuid.UUID, organization_id: uuid.UUID) -> None:
        link = await self._links.get(link_id, organization_id)
        if link is None:
            raise NotFoundException(f"Link {link_id} no encontrado")
        await self._links.delete(link)
        await self._session.commit()
