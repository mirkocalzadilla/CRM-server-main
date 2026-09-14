"""Acceso a `card_service` — servicios del catálogo asignados a una oportunidad (#132).

Siempre org-scoped. La lectura resuelve nombre/precio desde el catálogo vivo (`service`,
incl. soft-deleted, para que el historial no pierda la referencia)."""

from __future__ import annotations

import uuid
from typing import NamedTuple

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from server.modules.agent.domain.catalog_models import Service
from server.modules.crm.domain.models import CardService


class CardServiceRow(NamedTuple):
    id: uuid.UUID  # id del card_service
    service_id: uuid.UUID
    nombre: str
    precio: str
    moneda: str
    source: str


class CardServiceRepository:
    def __init__(self, session: AsyncSession) -> None:
        self.session = session

    async def list_for_card(
        self, card_id: uuid.UUID, organization_id: uuid.UUID
    ) -> list[CardServiceRow]:
        """Servicios de la card, resueltos contra el catálogo vivo, asc por alta."""
        result = await self.session.execute(
            select(
                CardService.id,
                CardService.service_id,
                Service.nombre,
                Service.precio,
                Service.moneda,
                CardService.source,
            )
            .join(Service, Service.id == CardService.service_id)
            .where(
                CardService.card_id == card_id,
                CardService.organization_id == organization_id,
            )
            .order_by(CardService.created_at)
        )
        return [CardServiceRow(*row) for row in result.all()]

    async def card_ids_with_service(
        self, card_ids: list[uuid.UUID], organization_id: uuid.UUID
    ) -> set[uuid.UUID]:
        """De los `card_ids` dados, cuáles tienen al menos un servicio enlazado (asignado
        o capturado). Batch para el board — evita un query por card (#206)."""
        if not card_ids:
            return set()
        result = await self.session.execute(
            select(CardService.card_id).where(
                CardService.card_id.in_(card_ids),
                CardService.organization_id == organization_id,
            )
        )
        return set(result.scalars().all())

    async def service_names_by_card(
        self, card_ids: list[uuid.UUID], organization_id: uuid.UUID
    ) -> dict[uuid.UUID, list[str]]:
        """Nombres de los servicios enlazados de cada card (asc por alta). Batch para
        el export de leads (#176) — evita un query por card."""
        if not card_ids:
            return {}
        result = await self.session.execute(
            select(CardService.card_id, Service.nombre)
            .join(Service, Service.id == CardService.service_id)
            .where(
                CardService.card_id.in_(card_ids),
                CardService.organization_id == organization_id,
            )
            .order_by(CardService.created_at)
        )
        names: dict[uuid.UUID, list[str]] = {}
        for card_id, nombre in result.tuples().all():
            names.setdefault(card_id, []).append(nombre)
        return names

    async def existing_service_ids(
        self, service_ids: list[uuid.UUID], organization_id: uuid.UUID
    ) -> set[uuid.UUID]:
        """De los `service_ids` dados, cuáles existen en el catálogo de la organización."""
        if not service_ids:
            return set()
        result = await self.session.execute(
            select(Service.id).where(
                Service.id.in_(service_ids),
                Service.organization_id == organization_id,
            )
        )
        return set(result.scalars().all())

    async def capture(
        self, card_id: uuid.UUID, organization_id: uuid.UUID, service_id: uuid.UUID
    ) -> None:
        """Estampa un servicio elegido por el bot (`source='captured'`, #133). El caller
        ya verificó que no esté enlazado (unique `(card_id, service_id)`)."""
        self.session.add(
            CardService(
                organization_id=organization_id,
                card_id=card_id,
                service_id=service_id,
                source="captured",
            )
        )
        await self.session.flush()

    async def set_assigned(
        self, card_id: uuid.UUID, organization_id: uuid.UUID, service_ids: list[uuid.UUID]
    ) -> None:
        """Reconcilia los servicios `assigned` de la card con `service_ids` (idempotente):
        agrega los que faltan, quita los `assigned` que sobran y **deja intactos los
        `captured`** del bot. Un servicio ya capturado no se duplica."""
        existing = (
            (
                await self.session.execute(
                    select(CardService).where(
                        CardService.card_id == card_id,
                        CardService.organization_id == organization_id,
                    )
                )
            )
            .scalars()
            .all()
        )
        linked = {row.service_id for row in existing}
        target = set(service_ids)
        for row in existing:
            if row.source == "assigned" and row.service_id not in target:
                await self.session.delete(row)
        for service_id in service_ids:
            if service_id not in linked:
                self.session.add(
                    CardService(
                        organization_id=organization_id,
                        card_id=card_id,
                        service_id=service_id,
                        source="assigned",
                    )
                )
        await self.session.flush()
