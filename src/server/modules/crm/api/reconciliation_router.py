"""API de la conciliación de pagos (`/crm/payments`).

La cola de lo que el sistema aprobó y todavía nadie cotejó contra el banco, más las dos
acciones sobre cada caso. Misma convención de errores que el resto del router del CRM
(captura explícita → 400/404) y los 3 roles del CRM: conciliar es operación diaria.
"""

from __future__ import annotations

import csv
import io
import uuid
from datetime import date

from fastapi import APIRouter, HTTPException, status
from fastapi.responses import StreamingResponse

from server.modules.core.api.deps import CurrentUser, DbSession
from server.modules.crm.api.reconciliation_schemas import (
    PendingPaymentOut,
    PendingPaymentsOut,
    RejectPaymentIn,
)
from server.modules.crm.domain.payment_models import PaymentReceipt
from server.modules.crm.services.payment_reconciliation_service import (
    PaymentReconciliationService,
)
from server.shared.exceptions import NotFoundException, ValidationException
from server.shared.pubsub import publisher
from server.shared.timezone import business_today

router = APIRouter(tags=["crm"])

_CSV_HEADERS = (
    "fecha_registro",
    "monto",
    "moneda",
    "fecha_pago",
    "beneficiario",
    "transaccion",
    "banco",
    "card_id",
    "estado",
)


def _service(session: DbSession) -> PaymentReconciliationService:
    return PaymentReconciliationService(session=session, publisher=publisher)


def _to_out(receipt: PaymentReceipt) -> PendingPaymentOut:
    extracted = receipt.extracted or {}
    return PendingPaymentOut(
        id=receipt.id,
        card_id=receipt.card_id,
        amount=str(receipt.amount) if receipt.amount is not None else None,
        currency=receipt.currency,
        paid_at=receipt.paid_at,
        beneficiary=receipt.beneficiary,
        reference=receipt.reference,
        bank=receipt.bank or (str(extracted.get("bank")) if extracted.get("bank") else None),
        created_at=receipt.created_at,
    )


@router.get("/payments/pending", response_model=PendingPaymentsOut)
async def list_pending(ctx: CurrentUser, session: DbSession) -> PendingPaymentsOut:
    """Pagos aprobados por el sistema que faltan cotejar con el banco."""
    receipts = await _service(session).pending(ctx.tenant.id)
    return PendingPaymentsOut(total=len(receipts), items=[_to_out(receipt) for receipt in receipts])


@router.post("/payments/{receipt_id}/confirm", response_model=PendingPaymentOut)
async def confirm_payment(
    receipt_id: uuid.UUID, ctx: CurrentUser, session: DbSession
) -> PendingPaymentOut:
    """Sella el pago como cotejado contra el banco. Idempotente."""
    try:
        receipt = await _service(session).confirm(receipt_id, ctx.tenant.id, ctx.user.id)
    except NotFoundException as exc:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=exc.message) from exc
    return _to_out(receipt)


@router.post("/payments/{receipt_id}/reject", response_model=PendingPaymentOut)
async def reject_payment(
    receipt_id: uuid.UUID, payload: RejectPaymentIn, ctx: CurrentUser, session: DbSession
) -> PendingPaymentOut:
    """El pago no entró: revoca la entrada y cierra la oportunidad como perdida.

    Revierte algo que ya se entregó, así que exige el motivo por escrito.
    """
    try:
        receipt = await _service(session).reject(
            receipt_id, ctx.tenant.id, ctx.user.id, payload.note
        )
    except NotFoundException as exc:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=exc.message) from exc
    except ValidationException as exc:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=exc.message) from exc
    return _to_out(receipt)


@router.get("/payments/export")
async def export_payments(
    ctx: CurrentUser, session: DbSession, day: date | None = None
) -> StreamingResponse:
    """CSV de los pagos auto-validados de un día (por defecto, hoy en Bolivia).

    "Hoy" es el día del negocio: `datetime.now(UTC).date()` ya es mañana desde las 20:00
    locales, así que abrir el export a la tarde devolvía el día siguiente.
    """
    target = day or business_today()
    receipts = await _service(session).confirmed_on(ctx.tenant.id, target)
    buffer = io.StringIO()
    writer = csv.writer(buffer)
    writer.writerow(_CSV_HEADERS)
    for receipt in receipts:
        writer.writerow(
            [
                receipt.created_at.isoformat(),
                receipt.amount if receipt.amount is not None else "",
                receipt.currency or "",
                receipt.paid_at.isoformat() if receipt.paid_at is not None else "",
                receipt.beneficiary or "",
                receipt.reference or "",
                receipt.bank or "",
                str(receipt.card_id),
                _status_of(receipt),
            ]
        )
    buffer.seek(0)
    filename = f"pagos-{target.isoformat()}.csv"
    return StreamingResponse(
        iter([buffer.getvalue()]),
        media_type="text/csv",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


def _status_of(receipt: PaymentReceipt) -> str:
    if receipt.human_rejected_at is not None:
        return "rechazado"
    if receipt.human_confirmed_at is not None:
        return "confirmado"
    return "por confirmar"
