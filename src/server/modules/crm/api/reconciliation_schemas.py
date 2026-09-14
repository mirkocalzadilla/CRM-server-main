"""Schemas de la conciliación de pagos."""

from __future__ import annotations

import uuid
from datetime import date, datetime

from pydantic import BaseModel, Field, field_validator

NOTE_MAX = 2000


class PendingPaymentOut(BaseModel):
    """Un pago de la cola, con lo necesario para cotejarlo contra el extracto."""

    id: uuid.UUID
    card_id: uuid.UUID
    amount: str | None
    currency: str | None
    paid_at: date | None
    beneficiary: str | None
    reference: str | None
    bank: str | None
    created_at: datetime


class PendingPaymentsOut(BaseModel):
    """La cola con su contador (el CRM lo muestra al lado del acceso)."""

    total: int
    items: list[PendingPaymentOut]


class RejectPaymentIn(BaseModel):
    """Rechazo de un pago: revoca la entrada, así que el motivo es obligatorio."""

    note: str = Field(min_length=3, max_length=NOTE_MAX)

    @field_validator("note")
    @classmethod
    def _clean(cls, value: str) -> str:
        cleaned = " ".join(value.split())
        if len(cleaned) < 3:
            raise ValueError(
                "La nota es obligatoria: explicá por qué el pago no se pudo confirmar."
            )
        return cleaned
