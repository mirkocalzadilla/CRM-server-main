"""Config de pagos de la organización: lectura, edición y resolución con fallback.

`resolve_qr_url` es el punto único que decide qué QR de pago recibe un lead: el de la
organización si lo cargó, y si no el global de la plataforma (`settings.payment_qr_url`).
El fallback es deliberado: prod venía funcionando con la env var y un tenant sin
configurar debe seguir mandando el QR correcto, no un mensaje vacío.

El QR propio ahora es un archivo subido acá (`payment_qr_store`), no una URL pegada a
mano: `payment_qr_url` sigue siendo lo que se manda —el agente no cambió— y
`payment_qr_storage_ref` dice cuál es el archivo nuestro. Hay **uno solo por
organización**, así que toda escritura del QR borra el archivo que quedó sin
referencia; siempre después de commitear, para que un fallo de base no borre la
imagen que todavía está en uso.
"""

from __future__ import annotations

import uuid

from sqlalchemy.ext.asyncio import AsyncSession

from server.config import get_settings
from server.modules.crm.api.payment_schemas import PaymentSettingsRead, PaymentSettingsUpdate
from server.modules.crm.domain.payment_models import PaymentSettings
from server.modules.crm.repositories.payment_settings_repository import (
    PaymentSettingsRepository,
)
from server.modules.crm.services.payment_qr_store import delete_payment_qr, store_payment_qr


class PaymentSettingsService:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session
        self._repo = PaymentSettingsRepository(session)

    async def resolve_qr_url(self, organization_id: uuid.UUID) -> str:
        """URL del QR de pago para la organización, con fallback al global."""
        row = await self._repo.get(organization_id)
        if row is not None and row.payment_qr_url:
            return row.payment_qr_url
        return get_settings().payment_qr_url

    async def expected_beneficiary(self, organization_id: uuid.UUID) -> str | None:
        """Nombre esperado del destinatario del comprobante. `None` = sin configurar."""
        row = await self._repo.get(organization_id)
        return row.expected_beneficiary if row is not None else None

    async def read(self, organization_id: uuid.UUID) -> PaymentSettingsRead:
        row = await self._repo.get(organization_id)
        custom_url = row.payment_qr_url if row is not None else None
        storage_ref = row.payment_qr_storage_ref if row is not None else None
        return PaymentSettingsRead(
            expected_beneficiary=row.expected_beneficiary if row is not None else None,
            payment_qr_url=custom_url or get_settings().payment_qr_url,
            is_qr_url_custom=bool(custom_url),
            is_qr_uploaded=bool(custom_url and storage_ref),
        )

    async def update(
        self, organization_id: uuid.UUID, payload: PaymentSettingsUpdate
    ) -> PaymentSettingsRead:
        row = await self._ensure_row(organization_id)
        fields = payload.model_dump(exclude_unset=True)
        # Apuntar la URL a otra imagen descarta el archivo subido, que si no quedaría
        # huérfano en disco. Reenviar la **misma** URL no: un cliente que manda de
        # vuelta lo que leyó estaría borrando la imagen que la fila sigue usando.
        changes_url = "payment_qr_url" in fields and fields["payment_qr_url"] != row.payment_qr_url
        stale_ref = row.payment_qr_storage_ref if changes_url else None
        for field, value in fields.items():
            setattr(row, field, value)
        if stale_ref is not None:
            row.payment_qr_storage_ref = None
        await self._session.commit()
        if stale_ref:
            delete_payment_qr(stale_ref)
        return await self.read(organization_id)

    async def set_qr_image(
        self, organization_id: uuid.UUID, *, content_type: str | None, data: bytes
    ) -> PaymentSettingsRead:
        """Deja la imagen subida como QR de la organización. Uno solo por tenant: el
        anterior se borra recién después de commitear el nuevo, así un fallo de base
        no deja a la organización sin QR."""
        stored = store_payment_qr(
            organization_id=organization_id, content_type=content_type, data=data
        )
        row = await self._ensure_row(organization_id)
        previous = row.payment_qr_storage_ref
        row.payment_qr_url = stored.url
        row.payment_qr_storage_ref = stored.storage_ref
        await self._session.commit()
        if previous:
            delete_payment_qr(previous)
        return await self.read(organization_id)

    async def clear_qr(self, organization_id: uuid.UUID) -> PaymentSettingsRead:
        """Saca el QR propio y vuelve al global de la plataforma."""
        row = await self._repo.get(organization_id)
        if row is None:
            return await self.read(organization_id)
        previous = row.payment_qr_storage_ref
        row.payment_qr_url = None
        row.payment_qr_storage_ref = None
        await self._session.commit()
        if previous:
            delete_payment_qr(previous)
        return await self.read(organization_id)

    async def _ensure_row(self, organization_id: uuid.UUID) -> PaymentSettings:
        row = await self._repo.get(organization_id)
        if row is None:
            row = await self._repo.add(PaymentSettings(organization_id=organization_id))
        return row
