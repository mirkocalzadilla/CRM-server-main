"""API del control de acceso en la puerta (`/crm/entries`).

Los 3 roles del CRM: quien atiende la puerta suele ser `staff`, y es justamente quien
necesita esto. El tenant se valida por JOIN a la card, porque `qr_entry` no tiene
`organization_id` propio.

**Un escaneo nunca devuelve un error HTTP por rechazar una entrada.** Siempre responde
200 con el motivo: en la puerta hay una fila esperando, y quien atiende necesita leer qué
pasó — no un código de estado que su navegador interprete como una falla de red.
"""

from __future__ import annotations

import uuid

from fastapi import APIRouter

from server.modules.core.api.deps import CurrentUser, DbSession
from server.modules.crm.api.redemption_schemas import (
    AttendanceOut,
    AttendeeOut,
    CheckInIn,
    RedeemIn,
    RedeemOut,
)
from server.modules.crm.repositories.attendance_repository import AttendanceRepository
from server.modules.crm.services.entry_admission import RedeemResult
from server.modules.crm.services.redemption_service import RedemptionService

router = APIRouter(tags=["crm"])


@router.post("/entries/redeem", response_model=RedeemOut)
async def redeem_entry(payload: RedeemIn, ctx: CurrentUser, session: DbSession) -> RedeemOut:
    """Valida y consume una entrada. Responde 200 siempre, con el motivo."""
    result = await RedemptionService(session=session).redeem(
        payload.token, ctx.tenant.id, ctx.user.id, payload.event_id
    )
    return _to_out(result)


@router.post("/entries/{entry_id}/check-in", response_model=RedeemOut)
async def check_in_entry(
    entry_id: uuid.UUID, payload: CheckInIn, ctx: CurrentUser, session: DbSession
) -> RedeemOut:
    """Admite a alguien desde la lista de asistencia, sin escanear.

    Es la vía de escape cuando la cámara no sirve — permiso denegado, mala luz, un QR
    arrugado — y por eso rechaza por los mismos motivos que el escaneo: existe porque
    falló la cámara, no para saltear una entrada anulada. Queda marcada como manual.
    """
    result = await RedemptionService(session=session).check_in(
        entry_id, ctx.tenant.id, ctx.user.id, payload.event_id
    )
    return _to_out(result)


def _to_out(result: RedeemResult) -> RedeemOut:
    return RedeemOut(
        status=str(result.status),
        admitted=result.admitted,
        detail=result.detail,
        lead_name=result.lead_name,
        service_name=result.service_name,
        amount=result.amount,
        event_name=result.event_name,
        used_at=result.used_at,
    )


@router.get("/entries/attendance/{event_id}", response_model=AttendanceOut)
async def event_attendance(
    event_id: uuid.UUID, ctx: CurrentUser, session: DbSession
) -> AttendanceOut:
    """Lista de asistencia del evento: quién tiene entrada y quién ya entró.

    Es el fallback cuando la cámara no funciona — un teléfono sin permiso, mala luz, un
    QR arrugado. Sin esto, un problema técnico deja a alguien afuera de un evento que
    pagó.
    """
    rows = await AttendanceRepository(session).for_event(event_id, ctx.tenant.id)
    attendees = [
        AttendeeOut(
            entry_id=row.entry_id,
            card_id=row.card_id,
            lead_name=row.lead_name,
            used_at=row.used_at,
            used_manually=row.used_manually,
            revoked_at=row.revoked_at,
        )
        for row in rows
    ]
    return AttendanceOut(
        total=len([a for a in attendees if a.revoked_at is None]),
        checked_in=len([a for a in attendees if a.used_at is not None]),
        attendees=attendees,
    )
