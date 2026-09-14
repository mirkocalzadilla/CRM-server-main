"""Admitir a alguien en la puerta del evento: escaneando el QR o a mano en la lista.

**Cada rechazo explica el motivo.** Quien atiende tiene una fila esperando y necesita
saber qué hacer con la persona que tiene enfrente. "Ya la usaron a las 19:40", "esa
entrada es del sábado" y "eso es el QR de pago, pedile la entrada" llevan a tres
conversaciones distintas; un "inválido" genérico no sirve para ninguna.

Los dos caminos comparten los mismos rechazos a propósito (ver `entry_admission`): el
manual existe porque la cámara puede fallar, no para saltear una entrada anulada.
"""

from __future__ import annotations

import uuid

from sqlalchemy.ext.asyncio import AsyncSession

from server.modules.crm.domain.models import Card, QrEntry
from server.modules.crm.domain.redemption import (
    RedeemStatus,
    looks_like_entry_token,
    looks_like_payment_qr,
)
from server.modules.crm.services.entry_admission import (
    EntryAdmission,
    EntryContext,
    RedeemResult,
    hhmm,
    result_for,
)
from server.shared.logger import get_logger

logger = get_logger(__name__)

_NOT_AN_ENTRY = "Ese código no es una entrada válida."
_NOT_FOUND = "No encontramos esa entrada. Revisá que sea la que le mandamos."
_PAYMENT_QR = "Eso es el QR de pago del banco: pedile la entrada que le mandamos."


class RedemptionService:
    def __init__(self, *, session: AsyncSession) -> None:
        self._session = session
        self._admission = EntryAdmission(session)

    async def redeem(
        self, raw_token: str, org_id: uuid.UUID, user_id: uuid.UUID, event_id: uuid.UUID | None
    ) -> RedeemResult:
        """Escaneo: valida lo que leyó la cámara y consume la entrada."""
        token = raw_token.strip()
        if not looks_like_entry_token(token):
            # Antes de decir "no existe", ver si lo escaneado es el QR de pago: es el
            # error más probable en la puerta y tiene arreglo inmediato.
            if looks_like_payment_qr(token):
                return RedeemResult(status=RedeemStatus.PAYMENT_QR, detail=_PAYMENT_QR)
            return RedeemResult(status=RedeemStatus.NOT_FOUND, detail=_NOT_AN_ENTRY)

        row = await self._admission.by_token(token, org_id)
        if row is None:
            # Una entrada de otra organización se ve igual que una que no existe — que es
            # exactamente lo que hay que responder.
            return RedeemResult(status=RedeemStatus.NOT_FOUND, detail=_NOT_FOUND)
        return await self._admit(row, org_id, user_id, event_id, manual=False)

    async def check_in(
        self, entry_id: uuid.UUID, org_id: uuid.UUID, user_id: uuid.UUID, event_id: uuid.UUID | None
    ) -> RedeemResult:
        """Check-in manual desde la lista de asistencia, cuando la cámara no sirve.

        Se llega acá con el `entry_id` de la lista, no con el token: los tokens no salen
        de la base, porque son el secreto que hace válido al QR.
        """
        row = await self._admission.by_id(entry_id, org_id)
        if row is None:
            return RedeemResult(status=RedeemStatus.NOT_FOUND, detail=_NOT_FOUND)
        return await self._admit(row, org_id, user_id, event_id, manual=True)

    async def _admit(
        self,
        row: tuple[QrEntry, Card],
        org_id: uuid.UUID,
        user_id: uuid.UUID,
        event_id: uuid.UUID | None,
        *,
        manual: bool,
    ) -> RedeemResult:
        entry, card = row
        context = await self._admission.context(entry, card, org_id)

        rejection = self._rejection(entry, context, event_id)
        if rejection is not None:
            return rejection

        if not await self._admission.consume(entry.id, user_id, manual=manual):
            # Perdió la carrera contra otra admisión simultánea. Se re-lee para informar
            # la hora real del uso que ganó.
            fresh = await self._admission.by_id(entry.id, org_id)
            used_at = fresh[0].used_at if fresh is not None else None
            logger.info("entry.admit_race_lost", entry_id=str(entry.id), manual=manual)
            return result_for(
                RedeemStatus.ALREADY_USED,
                context,
                f"Esta entrada ya se usó a las {hhmm(used_at)}.",
                used_at=used_at,
            )

        if entry.event_id is None:
            # Emitida antes de que los eventos existieran: es legítima, pasa con aviso.
            logger.info("entry.admitted_legacy", entry_id=str(entry.id), manual=manual)
            return result_for(
                RedeemStatus.LEGACY,
                context,
                "Entrada anterior al sistema de eventos: verificá a mano que sea de este.",
            )
        logger.info("entry.admitted", entry_id=str(entry.id), user_id=str(user_id), manual=manual)
        return result_for(RedeemStatus.OK, context, "Entrada válida.")

    def _rejection(
        self, entry: QrEntry, context: EntryContext, event_id: uuid.UUID | None
    ) -> RedeemResult | None:
        """Los motivos por los que no pasa. Iguales para el escaneo y para el manual."""
        if entry.revoked_at is not None:
            return result_for(
                RedeemStatus.REVOKED,
                context,
                "El pago de esta entrada no se pudo confirmar, así que fue anulada.",
            )
        if entry.used_at is not None:
            return result_for(
                RedeemStatus.ALREADY_USED,
                context,
                f"Esta entrada ya se usó a las {hhmm(entry.used_at)}.",
                used_at=entry.used_at,
            )
        if event_id is not None and entry.event_id is not None and entry.event_id != event_id:
            return result_for(
                RedeemStatus.WRONG_EVENT,
                context,
                f"Esta entrada es de otro evento: {context.event_name or 'otra fecha'}.",
            )
        return None
