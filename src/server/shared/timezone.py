"""La zona horaria del negocio.

Todo se guarda en UTC, y el proceso corre en UTC (la imagen no define `TZ`). Pero hay
texto que **una persona lee como hora de reloj** — el saludo del agente al lead, la hora
que el escáner informa en la puerta — y ahí UTC está cuatro horas adelantado: una entrada
usada a las 19:40 se informaría como "23:40", una hora que todavía no llegó.

Vive en `shared/` y no en cada módulo porque una zona duplicada es una zona que después
divergen: si el negocio se mueve o abre en otro país, esto es lo único que cambia.
"""

from __future__ import annotations

from datetime import UTC, date, datetime
from zoneinfo import ZoneInfo

BUSINESS_TZ = ZoneInfo("America/La_Paz")


def to_business_time(moment: datetime) -> datetime:
    """Convierte a la hora del negocio, la que se le muestra a una persona.

    Un valor sin zona se interpreta **como UTC**, no como la hora del proceso: todo lo que
    el sistema escribe es `datetime.now(UTC)`, y hay backends que devuelven la columna sin
    tzinfo. Dejarlo al default convertiría según dónde corra el proceso, que es justo el
    error que esto viene a arreglar.
    """
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=UTC)
    return moment.astimezone(BUSINESS_TZ)


def business_today() -> date:
    """Qué día es hoy para el negocio.

    `datetime.now(UTC).date()` ya es **mañana** entre las 20:00 y la medianoche de Bolivia,
    así que usarlo para "hoy" corre el día cada tarde: un export del día sale vacío y un
    umbral de antigüedad cuenta un día de más.
    """
    return datetime.now(BUSINESS_TZ).date()
