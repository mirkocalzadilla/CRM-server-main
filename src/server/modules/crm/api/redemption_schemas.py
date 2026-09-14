"""Schemas del escaneo de entradas en la puerta."""

from __future__ import annotations

import uuid
from datetime import datetime

from pydantic import BaseModel, Field

TOKEN_MAX = 2000


class RedeemIn(BaseModel):
    """Lo que la cámara leyó, tal cual.

    `token` es texto libre y no un UUID a propósito: en la puerta se escanea lo que sea,
    y parte del trabajo es **decirle a quien atiende qué escaneó** — un QR de pago
    confundido con una entrada es el error más probable, y responder "422 formato
    inválido" no resuelve nada. El largo generoso es porque el QR de pago del banco es un
    blob de cientos de caracteres.
    """

    token: str = Field(min_length=1, max_length=TOKEN_MAX)
    # El evento que se está controlando. Opcional: sin él se valida la entrada pero no se
    # puede rechazar por "es de otra fecha".
    event_id: uuid.UUID | None = None


class CheckInIn(BaseModel):
    """Check-in manual desde la lista: la entrada se identifica por la URL.

    No hay token acá a propósito: los tokens no salen de la base, porque son el secreto
    que hace válido al QR. Exponerlos en la lista de asistencia convertiría la pantalla
    de la puerta en una fuente de entradas copiables.
    """

    event_id: uuid.UUID | None = None


class RedeemOut(BaseModel):
    """Qué mostrar en la pantalla del escáner.

    `admitted` es lo que decide el color: verde si pasa, rojo/ámbar si no. `detail` es la
    frase que lee quien atiende, ya redactada para la situación.
    """

    status: str
    admitted: bool
    detail: str
    lead_name: str | None = None
    service_name: str | None = None
    amount: str | None = None
    event_name: str | None = None
    used_at: datetime | None = None


class AttendeeOut(BaseModel):
    """Una entrada del evento, para la lista de asistencia."""

    entry_id: uuid.UUID
    card_id: uuid.UUID
    lead_name: str
    used_at: datetime | None
    # Entró sin mostrar el QR. La lista lo distingue para que después del evento se pueda
    # revisar a quién se dejó pasar a mano.
    used_manually: bool
    revoked_at: datetime | None


class AttendanceOut(BaseModel):
    """La lista de asistencia con sus totales, para el fallback sin cámara."""

    total: int
    checked_in: int
    attendees: list[AttendeeOut]
