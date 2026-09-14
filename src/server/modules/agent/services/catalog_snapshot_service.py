"""Proyección del catálogo al snapshot que el agente lee en vivo (SPEC_catalogo §5.1).

Extraído de `catalog_service.py` (que llegó al límite de tamaño): acá viven los
proyectores puros y la re-sincronización, que corre **tras cada cambio** del catálogo
(#109) — no hay botón de "publicar" ni fase borrador, el runtime lee `agent.config`.

**Qué NO se proyecta, a propósito:** los links de entrega (`service_link`) y la
modalidad. El snapshot entra al contexto del LLM, y un link de grupo o de reunión ahí
es material que el modelo podría entregar antes de que el pago esté validado (o que
un lead consiga con una inyección de prompt). El fulfillment los lee de la DB, donde
el estado del pago sí se puede exigir. El precio numérico tampoco se proyecta: el
lead ya ve `precio` (texto display) y el monto estructurado solo sirve para validar
un comprobante, del lado del código.

**Los eventos SÍ se proyectan** (fecha y lugar de los próximos), y la diferencia con
los links es la que importa: la fecha de un curso es **información de venta** — el lead
pregunta "cuándo es" antes de pagar y el bot tiene que poder contestarle. Un link de
acceso, en cambio, es lo que se entrega *después* de pagar. El cupo tampoco viaja: un
"quedan 2 lugares" que el modelo repita mal es peor que no decirlo.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from server.modules.agent.domain.catalog_models import Service, ServiceCategory
from server.modules.agent.repositories.agent_repository import AgentRepository
from server.modules.crm.domain.event_models import EVENT_CLOSED, Event


def project_service(service: Service) -> dict[str, object]:
    """Una entrada del snapshot `config.services` (SPEC_catalogo §5.1).

    El material ya no vive en el servicio (#235): se manda por categoría. Se expone
    `categoria_slug` para que el agente llame `enviar_material` con la clave estable
    de la categoría del servicio.
    """
    category = service.category
    return {
        "slug": service.slug,
        "nombre": service.nombre,
        "categoria": category.nombre if category is not None else None,
        "categoria_slug": category.slug if category is not None else None,
        "resumen": service.resumen,
        "detalle": service.detalle,
        "precio": service.precio,
        "moneda": service.moneda,
        "flujo_cierre": service.flujo_cierre,
    }


def project_event(event: Event) -> dict[str, object]:
    """Una entrada del snapshot `config.events`: cuándo y dónde es cada curso.

    Es lo que le permite al bot contestar "cuándo es" y "dónde queda" sin inventar. Va
    el `service_slug` para que pueda relacionarlo con el servicio del que le preguntan.
    El cupo no viaja a propósito: un "quedan 2 lugares" que el modelo repita mal es
    peor que no decir nada.
    """
    return {
        "service_slug": event.service.slug if event.service is not None else None,
        "nombre": event.nombre,
        "fecha": event.starts_at.isoformat(),
        "lugar": event.location,
    }


def project_category(category: ServiceCategory) -> dict[str, object]:
    """Una entrada del snapshot `config.categories` (#235): el agente resuelve por
    `slug` los materiales a mandar cuando el lead elige la categoría."""
    return {
        "slug": category.slug,
        "nombre": category.nombre,
        "materials": [{"url": a.public_url, "filename": a.filename} for a in category.materials],
    }


class CatalogSnapshotService:
    """Re-proyecta el catálogo del tenant al `config` del agente que lo atiende."""

    def __init__(self, session: AsyncSession) -> None:
        self._session = session
        self._agents = AgentRepository(session)

    async def sync_for_tenant(self, organization_id: uuid.UUID) -> None:
        """Sincroniza resolviendo el agente del tenant por detrás. Lo usan los ABM
        que no conocen el agente (categorías #235, materiales, links)."""
        agents = await self._agents.list_for_organization(organization_id)
        if agents:  # MVP = un agente por tenant
            await self.sync(agents[0].id, organization_id)

    async def sync(self, agent_id: uuid.UUID, organization_id: uuid.UUID) -> None:
        """Re-proyecta los servicios **activos** + las categorías al snapshot que lee
        el agente en vivo. `populate_existing` evita proyectar colecciones stale."""
        agent = await self._agents.get(agent_id, organization_id)
        if agent is None:  # pragma: no cover - el caller ya validó el agente
            return
        result = await self._session.execute(
            select(Service)
            .where(
                Service.agent_id == agent_id,
                Service.organization_id == organization_id,
                Service.is_active.is_(True),
                Service.deleted_at.is_(None),
            )
            .order_by(Service.orden, Service.nombre)
            .options(selectinload(Service.category))
            .execution_options(populate_existing=True)
        )
        services = list(result.scalars().unique().all())
        cat_result = await self._session.execute(
            select(ServiceCategory)
            .where(ServiceCategory.organization_id == organization_id)
            .order_by(ServiceCategory.orden, ServiceCategory.nombre)
            .options(selectinload(ServiceCategory.materials))
            .execution_options(populate_existing=True)
        )
        categories = list(cat_result.scalars().unique().all())
        # Solo los eventos que todavía pueden venderse: uno cerrado o ya pasado en el
        # contexto solo sirve para que el bot ofrezca una fecha que no existe.
        event_result = await self._session.execute(
            select(Event)
            .where(
                Event.organization_id == organization_id,
                Event.status != EVENT_CLOSED,
                Event.starts_at >= datetime.now(UTC),
            )
            .order_by(Event.starts_at)
            .options(selectinload(Event.service))
            .execution_options(populate_existing=True)
        )
        events = list(event_result.scalars().unique().all())
        agent.config = {
            **(agent.config or {}),
            "services": [project_service(s) for s in services],
            "categories": [project_category(c) for c in categories],
            "events": [project_event(e) for e in events],
        }
        await self._session.commit()
