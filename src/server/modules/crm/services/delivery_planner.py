"""What to send for a card, resolved from the catalog and the agenda.

Shared by the automatic fulfillment and the CRM's manual "Generar entrada" button so the
two paths deliver the same message (server#290): `plan_delivery` decides the copy, this
resolves what it needs — the accepted service and its links, the event the entry binds
to (or why it cannot), and the date and place the lead reads.
"""

from __future__ import annotations

import uuid
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime

from sqlalchemy.ext.asyncio import AsyncSession

from server.modules.crm.domain import card_flags
from server.modules.crm.domain.delivery import (
    LINK_MAPS,
    MODALITY_HYBRID,
    MODALITY_PRESENCIAL,
    DeliveryPlan,
    EventInfo,
    LinkRef,
    plan_delivery,
)
from server.modules.crm.domain.event_models import Event
from server.modules.crm.domain.models import Card, QrEntry
from server.modules.crm.repositories.card_delivery_repository import CardDeliveryRepository
from server.modules.crm.repositories.qr_entry_repository import QrEntryRepository
from server.modules.crm.services.event_service import EventService


@dataclass(frozen=True, slots=True)
class PlannedDelivery:
    """The plan plus what the entry needs: its event, and the entry if already issued."""

    plan: DeliveryPlan
    event: Event | None = None
    existing_entry: QrEntry | None = None

    @property
    def blocked_by(self) -> str | None:
        return self.plan.blocked_by


class DeliveryPlanner:
    def __init__(self, session: AsyncSession) -> None:
        self._delivery = CardDeliveryRepository(session)
        self._issued = QrEntryRepository(session)
        self._events = EventService(session)

    async def plan_for(self, card: Card, org_id: uuid.UUID) -> PlannedDelivery:
        """Delivery plan for the card, or a blocked plan carrying the notice code."""
        services = await self._delivery.services_for_card(card.id, org_id)
        if not services:
            return PlannedDelivery(DeliveryPlan(blocked_by=card_flags.NO_MODALITY))
        if len(services) > 1:
            # Two accepted services: nobody can tell which one to deliver.
            return PlannedDelivery(DeliveryPlan(blocked_by=card_flags.AMBIGUOUS_SERVICE))
        service = services[0]
        links = [LinkRef(kind=link.kind, url=link.url, label=link.label) for link in service.links]
        if service.modality not in (MODALITY_PRESENCIAL, MODALITY_HYBRID):
            return PlannedDelivery(plan_delivery(service.modality, links))

        # An already issued entry is resent as is and **does not go through the event gate
        # again** — same as the CRM button. The gate counts the event's live entries, and
        # this one is among them: re-evaluating it would reject the resend over a seat it
        # reserves itself, leaving a paid lead with no QR. It happens whenever the first
        # send failed on the 24h window, which is exactly what the retry exists for.
        existing = await self._issued.get_by_card(card.id)
        if existing is not None:
            event = await self._events.get(existing.event_id, org_id)
            info = _event_info(event, existing.event_snapshot)
        else:
            # An entry exists for a concrete event: without a date and place it is of no
            # use to the lead and cannot be validated at the door; with the capacity full
            # it would be someone with a valid QR and no seat.
            resolution = await self._events.resolve_for_delivery(service.id, org_id)
            if resolution.is_blocked:
                return PlannedDelivery(DeliveryPlan(blocked_by=resolution.blocked_by))
            event = resolution.event
            info = _event_info(event, {})
        if event is not None and event.maps_url:
            # The event's location overrides the service's: same course, another venue.
            links = [link for link in links if link.kind != LINK_MAPS]
            links.append(LinkRef(kind=LINK_MAPS, url=event.maps_url))
        plan = plan_delivery(service.modality, links, event=info)
        return PlannedDelivery(plan, event=event, existing_entry=existing)


def _event_info(event: Event | None, snapshot: Mapping[str, object]) -> EventInfo | None:
    """Date and place for the message: the live event, or the copy the entry kept."""
    if event is not None:
        return EventInfo(starts_at=event.starts_at, location=event.location)
    raw = snapshot.get("starts_at")
    if not isinstance(raw, str):
        return None
    try:
        starts_at = datetime.fromisoformat(raw)
    except ValueError:
        return None
    location = snapshot.get("location")
    return EventInfo(starts_at=starts_at, location=str(location) if location else None)
