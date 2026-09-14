"""API de la config de pagos de la organización (`/crm/payment-settings`).

RBAC: `client_admin` + `platform_operator`, igual que el catálogo — el beneficiario
esperado y el QR de pago son configuración del negocio, no operación diaria de
inbox/tablero, así que `staff` no los edita (403). Errores por el handler global.
"""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends, File, UploadFile

from server.modules.agent.services.asset_service import MATERIAL_MAX_BYTES
from server.modules.core.api.deps import DbSession, require_roles
from server.modules.core.domain.models import TenantUserRole
from server.modules.core.domain.schemas import AuthenticatedUser
from server.modules.crm.api.payment_schemas import PaymentSettingsRead, PaymentSettingsUpdate
from server.modules.crm.services.payment_settings_service import PaymentSettingsService
from server.shared.exceptions import ValidationException

router = APIRouter(tags=["crm"])

PaymentConfigManager = Annotated[
    AuthenticatedUser, Depends(require_roles(TenantUserRole.CLIENT_ADMIN))
]


@router.get("/payment-settings", response_model=PaymentSettingsRead)
async def read_payment_settings(
    session: DbSession, ctx: PaymentConfigManager
) -> PaymentSettingsRead:
    return await PaymentSettingsService(session).read(ctx.tenant.id)


@router.put("/payment-settings", response_model=PaymentSettingsRead)
async def update_payment_settings(
    payload: PaymentSettingsUpdate, session: DbSession, ctx: PaymentConfigManager
) -> PaymentSettingsRead:
    return await PaymentSettingsService(session).update(ctx.tenant.id, payload)


@router.post("/payment-settings/qr", response_model=PaymentSettingsRead)
async def upload_payment_qr(
    session: DbSession, ctx: PaymentConfigManager, file: Annotated[UploadFile, File()]
) -> PaymentSettingsRead:
    """Sube la imagen del QR y la deja como la vigente, reemplazando la anterior: hay
    una sola por organización, y devolver la config completa evita un GET extra."""
    if file.size is not None and file.size > MATERIAL_MAX_BYTES:
        raise ValidationException("La imagen supera el límite de 5 MB")
    data = await file.read()
    return await PaymentSettingsService(session).set_qr_image(
        ctx.tenant.id, content_type=file.content_type, data=data
    )


@router.delete("/payment-settings/qr", response_model=PaymentSettingsRead)
async def remove_payment_qr(session: DbSession, ctx: PaymentConfigManager) -> PaymentSettingsRead:
    """Saca el QR propio; el bot vuelve a mandar el global de la plataforma."""
    return await PaymentSettingsService(session).clear_qr(ctx.tenant.id)
