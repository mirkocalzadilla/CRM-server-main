"""Acceso a `event` y al cupo de cada uno. Siempre tenant-scoped."""

from __future__ import annotations

import uuid
from datetime import datetime

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from server.modules.crm.domain.event_models import EVENT_ACTIVE, EVENT_SCHEDULED, Event
from server.modules.crm.domain.models import QrEntry


class EventRepository:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def get(self, event_id: uuid.UUID, organization_id: uuid.UUID) -> Event | None:
        result = await self._session.execute(
            select(Event).where(Event.id == event_id, Event.organization_id == organization_id)
        )
        return result.scalar_one_or_none()

    async def list_for_organization(
        self, organization_id: uuid.UUID, *, service_id: uuid.UUID | None = None
    ) -> list[Event]:
        """Eventos de la organización, del más próximo al más lejano."""
        query = select(Event).where(Event.organization_id == organization_id)
        if service_id is not None:
            query = query.where(Event.service_id == service_id)
        result = await self._session.execute(query.order_by(Event.starts_at))
        return list(result.scalars().all())

    async def current_for_service(
        self, service_id: uuid.UUID, organization_id: uuid.UUID, *, now: datetime
    ) -> Event | None:
        """El evento al que le corresponden las entradas que se emitan ahora.

        Se prefiere el que ya está `active`; si no hay, el próximo `scheduled` que no
        haya pasado. Un evento `closed` nunca recibe entradas nuevas, y uno cuya fecha
        ya pasó tampoco — emitir una entrada para algo que terminó es peor que no
        emitirla, porque el lead se enteraría en la puerta.
        """
        result = await self._session.execute(
            select(Event)
            .where(
                Event.organization_id == organization_id,
                Event.service_id == service_id,
                Event.status.in_((EVENT_ACTIVE, EVENT_SCHEDULED)),
                Event.starts_at >= now,
            )
            # `active` antes que `scheduled`, y entre iguales el más próximo.
            .order_by((Event.status == EVENT_ACTIVE).desc(), Event.starts_at)
            .limit(1)
        )
        return result.scalar_one_or_none()

    async def issued_count(self, event_id: uuid.UUID) -> int:
        """Entradas ya emitidas para el evento, sin contar las revocadas.

        Una revocada liberó su lugar: el pago detrás no se pudo confirmar, así que
        seguir reservándole cupo dejaría un asiento vacío que nadie puede usar.
        """
        result = await self._session.execute(
            select(func.count())
            .select_from(QrEntry)
            .where(QrEntry.event_id == event_id, QrEntry.revoked_at.is_(None))
        )
        return int(result.scalar_one())

    async def has_capacity(self, event: Event) -> bool:
        if event.capacity is None:
            return True
        return await self.issued_count(event.id) < event.capacity

    async def add(self, event: Event) -> Event:
        self._session.add(event)
        await self._session.flush()
        await self._session.refresh(event)
        return event

    async def delete(self, event: Event) -> None:
        await self._session.delete(event)
        await self._session.flush()
