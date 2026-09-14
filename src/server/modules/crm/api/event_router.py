"""API de la agenda de eventos (`/crm/agenda`).

**Por qué `/crm/agenda` y no `/crm/events`:** esa ruta ya existe y es el **stream SSE**
del CRM (`GET /crm/events?token=`), que el front consume con `EventSource`. Son dos
cosas sin relación que comparten la palabra "evento": una son notificaciones en tiempo
real, la otra son las fechas en las que hay curso. Renombrar el stream rompería el
front; esta superficie es nueva y no tiene consumidores, así que cede ella.

**RBAC asimétrico, y a propósito:** crear, editar y borrar un evento es configuración del
negocio (`client_admin` + `platform_operator`; `staff` → 403), porque define a qué le está
dando acceso una entrada. Pero **listarlos es operación de todos los días**: quien atiende
la puerta es `staff`, y necesita elegir qué evento está controlando para que el escáner
pueda rechazar una entrada por ser de otra fecha. Con la lista cerrada, el escáner nunca
manda `event_id` y ese rechazo simplemente no ocurre — el veredicto existe en el backend y
es inalcanzable desde la pantalla que lo usa.

Errores por el handler global, como el resto del catálogo.
"""

from __future__ import annotations

import uuid
from typing import Annotated

from fastapi import APIRouter, Depends, Response, status

from server.modules.core.api.deps import CurrentUser, DbSession, require_roles
from server.modules.core.domain.models import TenantUserRole
from server.modules.core.domain.schemas import AuthenticatedUser
from server.modules.crm.api.event_schemas import EventCreate, EventRead, EventUpdate
from server.modules.crm.services.delivery_trigger import retry_deliveries_blocked_by_event
from server.modules.crm.services.event_service import EventService

router = APIRouter(tags=["crm"])

EventManager = Annotated[AuthenticatedUser, Depends(require_roles(TenantUserRole.CLIENT_ADMIN))]


@router.get("/agenda", response_model=list[EventRead])
async def list_events(
    session: DbSession, ctx: CurrentUser, service_id: uuid.UUID | None = None
) -> list[EventRead]:
    """Eventos de la organización, del más próximo al más lejano.

    Abierto a los tres roles: es lo que el escáner de la puerta necesita para saber qué
    evento se está controlando. Un evento no tiene datos sensibles — nombre, fecha, lugar
    y cupo son justamente lo que quien atiende tiene que ver.
    """
    return await EventService(session).list_events(ctx.tenant.id, service_id=service_id)


@router.post("/agenda", response_model=EventRead, status_code=status.HTTP_201_CREATED)
async def create_event(payload: EventCreate, session: DbSession, ctx: EventManager) -> EventRead:
    created = await EventService(session).create_event(ctx.tenant.id, payload)
    # Las cards que esperaban esta fecha (`no_event`) se entregan en el mismo request:
    # cargar el evento es justamente lo que faltaba (server#290). Best-effort adentro.
    await retry_deliveries_blocked_by_event(session, created.service_id, ctx.tenant.id)
    return created


@router.put("/agenda/{event_id}", response_model=EventRead)
async def update_event(
    event_id: uuid.UUID, payload: EventUpdate, session: DbSession, ctx: EventManager
) -> EventRead:
    updated = await EventService(session).update_event(event_id, ctx.tenant.id, payload)
    # Editar puede destrabar lo que esperaba: más cupo, un estado o una fecha vigente.
    await retry_deliveries_blocked_by_event(session, updated.service_id, ctx.tenant.id)
    return updated


@router.delete(
    "/agenda/{event_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    response_class=Response,
    response_model=None,
)
async def delete_event(event_id: uuid.UUID, session: DbSession, ctx: EventManager) -> Response:
    """Borra el evento. Las entradas ya emitidas **no** se borran: quedan sin evento y
    el escáner las trata como legacy — el lead las tiene y son legítimas."""
    await EventService(session).delete_event(event_id, ctx.tenant.id)
    return Response(status_code=status.HTTP_204_NO_CONTENT)
