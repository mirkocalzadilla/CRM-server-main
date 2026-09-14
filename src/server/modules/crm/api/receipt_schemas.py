"""Schemas del comprobante de pago tal como los consume el CRM."""

from __future__ import annotations

import uuid
from datetime import datetime

from pydantic import BaseModel, Field, field_validator

NOTE_MAX = 2000


class ReceiptCheckOut(BaseModel):
    """Un check con su resultado, para el semáforo del panel."""

    code: str
    passed: bool
    detail: str


class ReceiptOut(BaseModel):
    """Comprobante con lo leído y lo decidido."""

    id: uuid.UUID
    card_id: uuid.UUID
    verdict: str  # 'pass' | 'fail'
    checks: list[ReceiptCheckOut]
    # Lo extraído, en crudo: monto, moneda, fecha, beneficiario, referencia, banco.
    extracted: dict[str, str | None]
    # URL pública de la imagen, para mostrarla al lado de los datos.
    image_url: str | None
    # Quién aprobó el pago y cuándo: `'system'` o el uuid del operador; `None` si nadie
    # todavía. Con esto el panel deja de ofrecer "validar" sobre un pago ya entregado.
    approved_at: datetime | None
    approved_by: str | None
    human_confirmed_at: datetime | None
    human_rejected_at: datetime | None
    human_note: str | None
    created_at: datetime


class ReceiptOverride(BaseModel):
    """Validación manual de un comprobante con checks en rojo.

    La nota es **obligatoria**: si un operador aprueba un pago que el sistema no
    aprobaría, tiene que quedar escrito por qué (mismo criterio que el motivo al
    descalificar, #253). Es lo único que después explica la decisión.
    """

    note: str = Field(min_length=3, max_length=NOTE_MAX)

    @field_validator("note")
    @classmethod
    def _clean(cls, value: str) -> str:
        cleaned = " ".join(value.split())
        if len(cleaned) < 3:
            raise ValueError("La nota es obligatoria: explicá por qué validás este pago.")
        return cleaned
