"""API del comprobante de pago en la card (`/crm/cards/{id}/receipt`).

Lo pueden operar los 3 roles del CRM, igual que el resto del tablero: validar un pago es
trabajo de la operación diaria, no configuración. Se sigue la convención de errores del
router del CRM (captura explícita → 400/404), no la del handler global.
"""

from __future__ import annotations

import uuid

from fastapi import APIRouter, HTTPException, status

from server.modules.core.api.deps import CurrentUser, DbSession
from server.modules.crm.api.receipt_schemas import ReceiptOut, ReceiptOverride
from server.modules.crm.services.receipt_review_service import ReceiptReviewService
from server.shared.exceptions import NotFoundException, ValidationException
from server.shared.pubsub import publisher

router = APIRouter(tags=["crm"])


def _service(session: DbSession) -> ReceiptReviewService:
    return ReceiptReviewService(session=session, publisher=publisher)


@router.get("/cards/{card_id}/receipt", response_model=ReceiptOut | None)
async def get_receipt(
    card_id: uuid.UUID, ctx: CurrentUser, session: DbSession
) -> ReceiptOut | None:
    """El comprobante más reciente de la card, o `null` si el lead no mandó ninguno."""
    return await _service(session).latest_for_card(card_id, ctx.tenant.id)


@router.post("/cards/{card_id}/receipt/validate", response_model=ReceiptOut | None)
async def validate_payment(
    card_id: uuid.UUID, ctx: CurrentUser, session: DbSession
) -> ReceiptOut | None:
    """Valida el pago y entrega, en una sola operación (un click).

    Solo para un comprobante cuyos checks pasaron: uno con checks en rojo se valida por
    `/receipt/override`, que exige la nota.
    """
    try:
        return await _service(session).validate_payment(card_id, ctx.tenant.id, ctx.user.id)
    except NotFoundException as exc:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=exc.message) from exc
    except ValidationException as exc:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=exc.message) from exc


@router.post("/cards/{card_id}/receipt/override", response_model=ReceiptOut | None)
async def override_payment(
    card_id: uuid.UUID, payload: ReceiptOverride, ctx: CurrentUser, session: DbSession
) -> ReceiptOut | None:
    """Valida un pago cuyos checks fallaron, con la nota que lo explica.

    La nota queda en el comprobante y en el motivo del movimiento de la card: es lo
    único que después explica por qué se aprobó algo que el sistema no aprobaría.
    """
    try:
        return await _service(session).validate_payment(
            card_id, ctx.tenant.id, ctx.user.id, note=payload.note
        )
    except NotFoundException as exc:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=exc.message) from exc
    except ValidationException as exc:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=exc.message) from exc
