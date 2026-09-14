"""Plomería compartida por las dos formas de admitir a alguien en la puerta.

Escanear el QR y marcar a alguien a mano en la lista son el mismo acto con dos entradas
distintas, y **tienen que rechazar por lo mismo**: una entrada anulada no puede pasar
porque la cámara no funcionó. Todo lo que decide eso vive acá, y los dos caminos lo usan.

**Un solo uso, garantizado por la base.** El marcado es un `UPDATE ... WHERE used_at IS
NULL` en una sola operación, y se decide por cuántas filas afectó. Leer-y-después-escribir
dejaría una ventana en la que dos personas admiten a la misma a la vez y las dos entran;
con el update condicional, exactamente una gana. En la puerta eso no es teórico: alguien
reenvía su QR por WhatsApp, o dos personas atienden la fila con dos teléfonos.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any, cast

from sqlalchemy import select, update
from sqlalchemy.engine import CursorResult
from sqlalchemy.ext.asyncio import AsyncSession

from server.modules.agent.repositories.conversation_repository import ConversationRepository
from server.modules.crm.domain.event_models import Event
from server.modules.crm.domain.models import Card, QrEntry
from server.modules.crm.domain.redemption import RedeemStatus
from server.modules.crm.repositories.card_service_repository import CardServiceRepository
from server.shared.timezone import to_business_time


@dataclass(frozen=True, slots=True)
class RedeemResult:
    """Qué mostrar en la pantalla de la puerta."""

    status: RedeemStatus
    # Datos del asistente, cuando hay entrada (aunque se rechace: saber de quién es la
    # entrada ya usada es justamente lo que resuelve la discusión en la puerta).
    lead_name: str | None = None
    service_name: str | None = None
    amount: str | None = None
    event_name: str | None = None
    used_at: datetime | None = None
    detail: str = ""

    @property
    def admitted(self) -> bool:
        return self.status in (RedeemStatus.OK, RedeemStatus.LEGACY)


@dataclass(frozen=True, slots=True)
class EntryContext:
    lead_name: str | None
    service_name: str | None
    amount: str | None
    event_name: str | None


class EntryAdmission:
    """Buscar la entrada, armar su contexto y consumirla. Sin decidir nada."""

    def __init__(self, session: AsyncSession) -> None:
        self._session = session
        self._conv = ConversationRepository(session)
        self._card_services = CardServiceRepository(session)

    async def by_token(self, token: str, org_id: uuid.UUID) -> tuple[QrEntry, Card] | None:
        return await self._find(QrEntry.token == token, org_id)

    async def by_id(self, entry_id: uuid.UUID, org_id: uuid.UUID) -> tuple[QrEntry, Card] | None:
        return await self._find(QrEntry.id == entry_id, org_id)

    async def _find(self, criterion: Any, org_id: uuid.UUID) -> tuple[QrEntry, Card] | None:
        """Tenant-scoped por JOIN a la card: `qr_entry` no tiene `organization_id` propio.

        `populate_existing` porque el estado de uso puede haber cambiado en otra sesión
        entre dos escaneos, y lo que se necesita es el de la base, no el del identity map.
        """
        result = await self._session.execute(
            select(QrEntry, Card)
            .join(Card, Card.id == QrEntry.card_id)
            .where(criterion, Card.organization_id == org_id)
            .execution_options(populate_existing=True)
        )
        found = result.first()
        return (found[0], found[1]) if found is not None else None

    async def consume(self, entry_id: uuid.UUID, user_id: uuid.UUID, *, manual: bool) -> bool:
        """Marca la entrada usada. `False` si otra admisión llegó primero.

        Una sola sentencia condicional: el `WHERE` es lo que hace que de dos admisiones
        simultáneas gane exactamente una, sin depender de una lectura previa.
        """
        statement = (
            update(QrEntry)
            .where(
                QrEntry.id == entry_id,
                QrEntry.used_at.is_(None),
                QrEntry.revoked_at.is_(None),
            )
            .values(used_at=datetime.now(UTC), used_by=user_id, used_manually=manual)
        )
        result = await self._session.execute(statement)
        await self._session.commit()
        # `rowcount` de un UPDATE es lo que decide la carrera: exactamente una fila
        # afectada = esta admisión ganó. El cast es porque el tipo estático de `execute`
        # es el genérico `Result`, pero un UPDATE siempre devuelve un `CursorResult`.
        return cast("CursorResult[Any]", result).rowcount == 1

    async def context(self, entry: QrEntry, card: Card, org_id: uuid.UUID) -> EntryContext:
        """Quién es y qué compró, para mostrarlo en la pantalla."""
        conversation = await self._conv.get_by_id(card.conversation_id, org_id)
        services = await self._card_services.list_for_card(card.id, org_id)
        event_name = str(entry.event_snapshot.get("nombre") or "") or None
        if entry.event_id is not None:
            event = await self._session.get(Event, entry.event_id)
            if event is not None:
                event_name = event.nombre
        return EntryContext(
            lead_name=(conversation.full_name if conversation is not None else None) or card.title,
            service_name=services[0].nombre if services else None,
            amount=services[0].precio if services else None,
            event_name=event_name,
        )


def result_for(
    status: RedeemStatus,
    ctx: EntryContext,
    detail: str,
    *,
    used_at: datetime | None = None,
) -> RedeemResult:
    return RedeemResult(
        status=status,
        lead_name=ctx.lead_name,
        service_name=ctx.service_name,
        amount=ctx.amount,
        event_name=ctx.event_name,
        used_at=used_at,
        detail=detail,
    )


def hhmm(moment: datetime | None) -> str:
    """La hora del negocio, que es la que lee quien está en la puerta.

    Explícitamente en la zona de Bolivia y no en la del proceso: el contenedor corre en
    UTC, y "ya se usó a las 23:40" para una entrada consumida a las 19:40 le da la razón
    a quien está discutiendo en la puerta con un QR reenviado.
    """
    return to_business_time(moment).strftime("%H:%M") if moment is not None else "recién"
