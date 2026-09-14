"""Acceso a `payment_receipt` — comprobantes con lo leído y lo decidido."""

from __future__ import annotations

import uuid
from datetime import UTC, date, datetime, time, timedelta

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from server.modules.crm.domain.payment_models import PaymentReceipt
from server.shared.timezone import BUSINESS_TZ


class PaymentReceiptRepository:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def get_by_wamid(self, wamid: str, organization_id: uuid.UUID) -> PaymentReceipt | None:
        """El comprobante ya procesado de ese mensaje. Hace el job idempotente."""
        result = await self._session.execute(
            select(PaymentReceipt).where(
                PaymentReceipt.wamid == wamid,
                PaymentReceipt.organization_id == organization_id,
            )
        )
        return result.scalar_one_or_none()

    async def get_by_sha(self, sha256: str, organization_id: uuid.UUID) -> PaymentReceipt | None:
        """Un comprobante con la misma imagen exacta, de cualquier card de la org.

        Es la defensa contra reenviar la misma captura: sirve incluso cuando el número
        de transacción no se pudo leer.
        """
        result = await self._session.execute(
            select(PaymentReceipt).where(
                PaymentReceipt.image_sha256 == sha256,
                PaymentReceipt.organization_id == organization_id,
            )
        )
        return result.scalar_one_or_none()

    async def get_by_reference(
        self, reference: str, organization_id: uuid.UUID
    ) -> PaymentReceipt | None:
        """Un comprobante con el mismo número de transacción, de cualquier card.

        La misma transferencia no puede pagar dos compras: acá se detecta que un lead
        reenvió el comprobante de otro (o el suyo, para una segunda inscripción).
        """
        result = await self._session.execute(
            select(PaymentReceipt).where(
                PaymentReceipt.reference == reference,
                PaymentReceipt.organization_id == organization_id,
            )
        )
        return result.scalar_one_or_none()

    async def get(self, receipt_id: uuid.UUID, organization_id: uuid.UUID) -> PaymentReceipt | None:
        result = await self._session.execute(
            select(PaymentReceipt).where(
                PaymentReceipt.id == receipt_id,
                PaymentReceipt.organization_id == organization_id,
            )
        )
        return result.scalar_one_or_none()

    async def latest_for_card(
        self, card_id: uuid.UUID, organization_id: uuid.UUID
    ) -> PaymentReceipt | None:
        """El comprobante más reciente de la card (el que mira el operador)."""
        result = await self._session.execute(
            select(PaymentReceipt)
            .where(
                PaymentReceipt.card_id == card_id,
                PaymentReceipt.organization_id == organization_id,
            )
            .order_by(PaymentReceipt.created_at.desc())
            .limit(1)
        )
        return result.scalar_one_or_none()

    async def list_pending_confirmation(self, organization_id: uuid.UUID) -> list[PaymentReceipt]:
        """Pagos que **el sistema** aprobó y que nadie cotejó todavía con el banco.

        Solo los auto-aprobados (`verdict = 'pass'` sin nota humana): un pago que una
        persona validó a mano ya pasó por ojos humanos, y ponerlo en la cola de
        conciliación sería pedir el mismo trabajo dos veces.
        """
        result = await self._session.execute(
            select(PaymentReceipt)
            .where(
                PaymentReceipt.organization_id == organization_id,
                PaymentReceipt.verdict == "pass",
                PaymentReceipt.human_note.is_(None),
                PaymentReceipt.human_confirmed_at.is_(None),
                PaymentReceipt.human_rejected_at.is_(None),
            )
            .order_by(PaymentReceipt.created_at)
        )
        return list(result.scalars().all())

    async def list_auto_approved_on(
        self, organization_id: uuid.UUID, day: date
    ) -> list[PaymentReceipt]:
        """Auto-aprobados de un día, para cotejar contra el extracto bancario.

        El día es el **día boliviano**, no el día UTC: quien concilia compara contra un
        extracto bancario de Bolivia. Con los límites en UTC, todo lo que entra después de
        las 20:00 locales caía en el CSV del día siguiente y faltaba justo en el que se
        estaba cotejando. Los límites se pasan convertidos a UTC porque es como está
        guardada la columna.
        """
        start = datetime.combine(day, time.min, tzinfo=BUSINESS_TZ).astimezone(UTC)
        end = start + timedelta(days=1)
        result = await self._session.execute(
            select(PaymentReceipt)
            .where(
                PaymentReceipt.organization_id == organization_id,
                PaymentReceipt.verdict == "pass",
                PaymentReceipt.human_note.is_(None),
                PaymentReceipt.created_at >= start,
                PaymentReceipt.created_at < end,
            )
            .order_by(PaymentReceipt.created_at)
        )
        return list(result.scalars().all())

    async def add(self, receipt: PaymentReceipt) -> PaymentReceipt:
        self._session.add(receipt)
        await self._session.flush()
        await self._session.refresh(receipt)
        return receipt
