"""Schemas de la config de pagos por organización."""

from __future__ import annotations

from pydantic import BaseModel, Field, field_validator

_BENEFICIARY_MAX = 120
_URL_MAX = 500


class PaymentSettingsRead(BaseModel):
    """Config vigente de la organización. `payment_qr_url` viene ya resuelta (si la
    organización no tiene una propia, es la global), y `is_qr_url_custom` dice si la
    cargó el operador o si está viendo el default de la plataforma.

    `is_qr_uploaded` distingue el QR **subido acá** (archivo nuestro, se puede
    reemplazar y borrar) de una URL externa configurada a mano, que el front solo
    puede mostrar o descartar."""

    expected_beneficiary: str | None
    payment_qr_url: str
    is_qr_url_custom: bool
    is_qr_uploaded: bool


class PaymentSettingsUpdate(BaseModel):
    """Edición parcial: solo los campos presentes se aplican; `null` explícito borra
    el valor (el QR vuelve al global, el beneficiario queda sin configurar)."""

    expected_beneficiary: str | None = Field(default=None, max_length=_BENEFICIARY_MAX)
    payment_qr_url: str | None = Field(default=None, max_length=_URL_MAX)

    @field_validator("expected_beneficiary")
    @classmethod
    def _clean_beneficiary(cls, value: str | None) -> str | None:
        if value is None:
            return None
        cleaned = " ".join(value.split())
        return cleaned or None

    @field_validator("payment_qr_url")
    @classmethod
    def _check_url(cls, value: str | None) -> str | None:
        if value is None:
            return None
        cleaned = value.strip()
        if not cleaned:
            return None
        if not cleaned.startswith(("http://", "https://")):
            raise ValueError("El link del QR debe empezar con http:// o https://")
        return cleaned
