"""CRUD del catálogo de servicios (SPEC_admin_catalogo_kb §5/§9).

Todo cambio re-proyecta el snapshot que lee el agente en vivo; esa proyección vive
en `CatalogSnapshotService` (#109: sin botón de "publicar" ni fase borrador — el
runtime lee `agent.config`, así que el cambio se ve al instante). Todo org-scoped.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from server.modules.agent.api.catalog_schemas import ServiceCreate, ServiceUpdate
from server.modules.agent.domain.catalog_models import Service
from server.modules.agent.domain.models import Agent
from server.modules.agent.repositories.agent_repository import AgentRepository
from server.modules.agent.repositories.service_category_repository import (
    ServiceCategoryRepository,
)
from server.modules.agent.repositories.service_repository import ServiceRepository
from server.modules.agent.services.catalog_snapshot_service import CatalogSnapshotService
from server.shared.exceptions import NotFoundException, ValidationException


class CatalogService:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session
        self._services = ServiceRepository(session)
        self._categories = ServiceCategoryRepository(session)
        self._agents = AgentRepository(session)
        self._snapshot = CatalogSnapshotService(session)

    async def _require_agent(self, agent_id: uuid.UUID, organization_id: uuid.UUID) -> Agent:
        agent = await self._agents.get(agent_id, organization_id)
        if agent is None:
            raise NotFoundException(f"Agente {agent_id} no encontrado")
        return agent

    async def _resolve_tenant_agent(self, organization_id: uuid.UUID) -> Agent | None:
        """El catálogo es del tenant, no del agente: resolvemos su agente por detrás.

        MVP = un agente por tenant (tomamos el primero). El client_admin administra el
        catálogo sin conocer ni ver la superficie del agente (config_router)."""
        agents = await self._agents.list_for_organization(organization_id)
        return agents[0] if agents else None

    async def _check_category(
        self, category_id: uuid.UUID | None, organization_id: uuid.UUID
    ) -> None:
        if category_id is None:
            return
        if await self._categories.get(category_id, organization_id) is None:
            raise ValidationException("La categoría referenciada no existe en esta organización")

    async def list_services(self, agent_id: uuid.UUID, organization_id: uuid.UUID) -> list[Service]:
        await self._require_agent(agent_id, organization_id)
        return await self._services.list_for_agent(agent_id, organization_id)

    async def list_services_for_tenant(self, organization_id: uuid.UUID) -> list[Service]:
        """Lista el catálogo del tenant (resuelve el agente por detrás). Sin agente
        configurado → catálogo vacío (no es un error para el operador)."""
        agent = await self._resolve_tenant_agent(organization_id)
        if agent is None:
            return []
        return await self._services.list_for_agent(agent.id, organization_id)

    async def create_service_for_tenant(
        self, organization_id: uuid.UUID, payload: ServiceCreate
    ) -> Service:
        """Crea un servicio en el catálogo del tenant (resuelve el agente por detrás)."""
        agent = await self._resolve_tenant_agent(organization_id)
        if agent is None:
            raise NotFoundException("No hay un agente configurado para este tenant")
        return await self.create_service(agent.id, organization_id, payload)

    async def create_service(
        self, agent_id: uuid.UUID, organization_id: uuid.UUID, payload: ServiceCreate
    ) -> Service:
        await self._require_agent(agent_id, organization_id)
        if await self._services.get_by_slug(agent_id, payload.slug) is not None:
            raise ValidationException(f"Ya existe un servicio con slug '{payload.slug}'")
        await self._check_category(payload.category_id, organization_id)
        service = await self._services.add(
            Service(organization_id=organization_id, agent_id=agent_id, **payload.model_dump())
        )
        await self._session.commit()
        await self._snapshot.sync(agent_id, organization_id)
        return await self._reload(service_id=service.id, organization_id=organization_id)

    async def update_service(
        self, service_id: uuid.UUID, organization_id: uuid.UUID, payload: ServiceUpdate
    ) -> Service:
        service = await self._services.get(service_id, organization_id)
        if service is None:
            raise NotFoundException(f"Servicio {service_id} no encontrado")
        agent_id = service.agent_id
        changes = payload.model_dump(exclude_unset=True)
        if "category_id" in changes:
            await self._check_category(changes["category_id"], organization_id)
        for field, value in changes.items():
            setattr(service, field, value)
        await self._session.commit()
        await self._snapshot.sync(agent_id, organization_id)
        return await self._reload(service_id=service.id, organization_id=organization_id)

    async def _reload(self, *, service_id: uuid.UUID, organization_id: uuid.UUID) -> Service:
        """Re-lee el servicio con su categoría fresca tras la escritura."""
        result = await self._session.execute(
            select(Service)
            .where(Service.id == service_id, Service.organization_id == organization_id)
            .execution_options(populate_existing=True)
        )
        service = result.scalar_one_or_none()
        if service is None:  # pragma: no cover - acabamos de escribirlo
            raise NotFoundException(f"Servicio {service_id} no encontrado")
        return service

    async def delete_service(self, service_id: uuid.UUID, organization_id: uuid.UUID) -> None:
        """Baja lógica (soft delete): marca `deleted_at` (no borra; conserva la
        referencia para el historial del contacto y `card_service`)."""
        service = await self._services.get(service_id, organization_id)
        if service is None:
            raise NotFoundException(f"Servicio {service_id} no encontrado")
        agent_id = service.agent_id
        service.deleted_at = datetime.now(UTC)
        await self._session.commit()
        await self._snapshot.sync(agent_id, organization_id)

    async def sync_tenant_snapshot(self, organization_id: uuid.UUID) -> None:
        """Delegación fina que conservan los ABM que no conocen el agente (categorías
        #235, materiales): la proyección vive en `CatalogSnapshotService`."""
        await self._snapshot.sync_for_tenant(organization_id)

    async def sync_snapshot(self, agent_id: uuid.UUID, organization_id: uuid.UUID) -> None:
        """Delegación fina para los callers que ya resolvieron el agente (seed)."""
        await self._snapshot.sync(agent_id, organization_id)
