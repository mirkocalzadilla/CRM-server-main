"""ABM de eventos + resolución del evento al que corresponde una entrada.

`resolve_for_delivery` es la pieza que consume el fulfillment: dado el servicio que el
lead compró, devuelve el evento al que se le emite la entrada, o **por qué no se puede**.
Los dos motivos posibles son avisos, no errores: sin evento cargado o con el cupo lleno,
la entrega la retoma un humano en vez de emitir una entrada inútil o de más.

**Cada alta, edición y baja re-proyecta el snapshot del agente.** Los eventos viajan al
contexto del bot a propósito (la fecha de un curso es lo primero que pregunta un lead,
*antes* de pagar), pero el snapshot solo se reconstruía al tocar el catálogo: cargar la
agenda de septiembre no la hacía visible hasta que alguien editara algún servicio. Y
cargar los eventos es un paso del checklist de deploy, así que el síntoma habría aparecido
el primer día — con el bot diciendo que no tiene fechas.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import UTC, datetime

from sqlalchemy.ext.asyncio import AsyncSession

from server.modules.agent.repositories.service_repository import ServiceRepository
from server.modules.agent.services.catalog_snapshot_service import CatalogSnapshotService
from server.modules.crm.api.event_schemas import EventCreate, EventRead, EventUpdate
from server.modules.crm.domain import card_flags
from server.modules.crm.domain.event_models import Event
from server.modules.crm.repositories.event_repository import EventRepository
from server.shared.exceptions import NotFoundException, ValidationException
from server.shared.logger import get_logger

logger = get_logger(__name__)


@dataclass(frozen=True, slots=True)
class EventResolution:
    """El evento al que emitir la entrada, o el aviso que lo impide."""

    event: Event | None = None
    blocked_by: str | None = None

    @property
    def is_blocked(self) -> bool:
        return self.blocked_by is not None


class EventService:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session
        self._events = EventRepository(session)
        self._services = ServiceRepository(session)
        self._snapshot = CatalogSnapshotService(session)

    async def list_events(
        self, organization_id: uuid.UUID, *, service_id: uuid.UUID | None = None
    ) -> list[EventRead]:
        events = await self._events.list_for_organization(organization_id, service_id=service_id)
        return [await self._to_read(event) for event in events]

    async def create_event(self, organization_id: uuid.UUID, payload: EventCreate) -> EventRead:
        if await self._services.get(payload.service_id, organization_id) is None:
            raise ValidationException("El servicio referenciado no existe en esta organización")
        event = await self._events.add(
            Event(organization_id=organization_id, **payload.model_dump())
        )
        await self._session.commit()
        await self._snapshot.sync_for_tenant(organization_id)
        return await self._to_read(event)

    async def update_event(
        self, event_id: uuid.UUID, organization_id: uuid.UUID, payload: EventUpdate
    ) -> EventRead:
        event = await self._require(event_id, organization_id)
        for field, value in payload.model_dump(exclude_unset=True).items():
            setattr(event, field, value)
        await self._session.commit()
        await self._snapshot.sync_for_tenant(organization_id)
        # `updated_at` lo pone la DB (`onupdate`): sin refresh queda expirado.
        await self._session.refresh(event)
        return await self._to_read(event)

    async def delete_event(self, event_id: uuid.UUID, organization_id: uuid.UUID) -> None:
        """Borra el evento. Las entradas emitidas quedan sin evento (FK `SET NULL`), no
        se borran: el lead ya las tiene y el escáner las tratará como legacy."""
        event = await self._require(event_id, organization_id)
        await self._events.delete(event)
        await self._session.commit()
        await self._snapshot.sync_for_tenant(organization_id)

    async def get(self, event_id: uuid.UUID | None, organization_id: uuid.UUID) -> Event | None:
        """El evento de una entrada ya emitida, si sigue existiendo.

        Acepta `None` porque una entrada legacy no tiene evento, y reenviarla igual es
        deliberado: ya está en el teléfono del lead.
        """
        if event_id is None:
            return None
        return await self._events.get(event_id, organization_id)

    async def resolve_for_delivery(
        self, service_id: uuid.UUID, organization_id: uuid.UUID, *, now: datetime | None = None
    ) -> EventResolution:
        """Evento al que emitir la entrada, o el aviso que lo impide."""
        event = await self._events.current_for_service(
            service_id, organization_id, now=now or datetime.now(UTC)
        )
        if event is None:
            # Emitir una entrada sin fecha ni lugar es darle al lead un papel que no
            # sirve, y que en la puerta no se puede validar contra nada.
            return EventResolution(blocked_by=card_flags.NO_EVENT)
        if not await self._events.has_capacity(event):
            # Nunca se emite una entrada de más en silencio: en la puerta habría alguien
            # con su QR válido y sin lugar.
            logger.info("event.capacity_full", event_id=str(event.id))
            return EventResolution(blocked_by=card_flags.CAPACITY_FULL)
        return EventResolution(event=event)

    async def _require(self, event_id: uuid.UUID, organization_id: uuid.UUID) -> Event:
        event = await self._events.get(event_id, organization_id)
        if event is None:
            raise NotFoundException(f"Evento {event_id} no encontrado")
        return event

    async def _to_read(self, event: Event) -> EventRead:
        read = EventRead.model_validate(event)
        return read.model_copy(update={"issued": await self._events.issued_count(event.id)})


def snapshot_of(event: Event) -> dict[str, object]:
    """Fecha y lugar tal como están al emitir la entrada.

    Se copia en la entrada a propósito: en la puerta tiene que poder leerse aunque
    después alguien edite el evento o lo borre.
    """
    return {
        "event_id": str(event.id),
        "nombre": event.nombre,
        "starts_at": event.starts_at.isoformat(),
        "location": event.location,
        "maps_url": event.maps_url,
    }


def blocked_message(flag: str | None) -> str:
    """El aviso, dicho para una persona que está esperando saber qué hacer.

    El flujo automático deja un código en la card; acá hay alguien mirando la pantalla,
    y "no_event" no le dice qué le falta hacer.
    """
    return _BLOCKED_MESSAGES.get(flag or "", _BLOCKED_MESSAGES[""])


_BLOCKED_MESSAGES: dict[str, str] = {
    card_flags.NO_EVENT: (
        "el servicio no tiene ningún evento próximo cargado: creá el evento y volvé a intentar"
    ),
    card_flags.CAPACITY_FULL: "el evento llegó a su cupo: no se pueden emitir más entradas",
    "": "no se puede generar la entrada para este servicio",
}
