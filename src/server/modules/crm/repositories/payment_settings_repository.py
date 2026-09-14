"""Acceso a `payment_settings` — una fila por organización."""

from __future__ import annotations

import uuid

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from server.modules.crm.domain.payment_models import PaymentSettings


class PaymentSettingsRepository:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def get(self, organization_id: uuid.UUID) -> PaymentSettings | None:
        result = await self._session.execute(
            select(PaymentSettings).where(PaymentSettings.organization_id == organization_id)
        )
        return result.scalar_one_or_none()

    async def add(self, settings: PaymentSettings) -> PaymentSettings:
        self._session.add(settings)
        await self._session.flush()
        await self._session.refresh(settings)
        return settings
