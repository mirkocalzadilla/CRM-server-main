"""Lecturas y escrituras que necesita la entrega automática de una card.

Separado de `CardServiceRepository` (que proyecta filas planas para el board y el
export) porque el fulfillment necesita las **entidades** `Service` con su modalidad y
sus links, no un resumen de nombre y precio.
"""

from __future__ import annotations

import uuid

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from server.modules.agent.domain.catalog_models import Service
from server.modules.crm.domain.models import Card, CardService


class CardDeliveryRepository:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def services_for_card(
        self, card_id: uuid.UUID, organization_id: uuid.UUID
    ) -> list[Service]:
        """Servicios aceptados de la card, con sus links cargados.

        Incluye los dados de baja lógica: si el operador borró el servicio del catálogo
        después de la venta, lo comprado sigue siendo eso y hay que poder entregarlo.
        """
        result = await self._session.execute(
            select(Service)
            .join(CardService, CardService.service_id == Service.id)
            .where(
                CardService.card_id == card_id,
                CardService.organization_id == organization_id,
            )
            .order_by(CardService.created_at)
            .options(selectinload(Service.links))
        )
        return list(result.scalars().unique().all())

    async def cards_in_stage_with_service(
        self, stage_id: uuid.UUID, service_id: uuid.UUID, organization_id: uuid.UUID
    ) -> list[Card]:
        """Cards de la org en `stage_id` que tienen aceptado `service_id`.

        Lo usa el reintento de entrega al cargar un evento: son pocas filas (las que
        están en "Pago validado" esperando esa fecha), así que el filtro por aviso se
        hace en Python y no sobre la columna JSON, que cada motor consulta distinto.
        """
        result = await self._session.execute(
            select(Card)
            .join(CardService, CardService.card_id == Card.id)
            .where(
                Card.organization_id == organization_id,
                Card.stage_id == stage_id,
                CardService.service_id == service_id,
            )
            .order_by(Card.created_at)
        )
        return list(result.scalars().unique().all())

    async def set_flags(self, card: Card, flags: list[str]) -> None:
        """Reemplaza los avisos de la card. El caller hace commit (capa servicio).

        Se reasigna la lista completa en vez de mutarla: SQLAlchemy no detecta mutación
        in-place de una columna JSON, así que un `append` no se persistiría.
        """
        card.flags = list(flags)
        await self._session.flush()
