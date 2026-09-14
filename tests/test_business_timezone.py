"""La zona horaria del negocio, y por qué existe un módulo para ella.

Todo se guarda en UTC y el contenedor corre en UTC. El problema aparece cuando un texto
lo lee **una persona como hora de reloj**: ahí UTC está cuatro horas adelantado, y entre
las 20:00 y la medianoche de Bolivia el *día* también.

Una revisión adversarial encontró este bug en dos pantallas distintas (la hora que informa
el escáner en la puerta y el día del CSV de conciliación) con la misma causa: no había una
sola fuente para la zona. Estos tests fijan el comportamiento de esa fuente única.
"""

from __future__ import annotations

from datetime import UTC, datetime
from unittest.mock import MagicMock

from server.modules.crm.services.receipt_validation_service import ReceiptValidationService
from server.shared.timezone import BUSINESS_TZ, business_today, to_business_time


def test_utc_evening_is_the_same_day_afternoon_in_bolivia() -> None:
    """23:40 UTC son las 19:40 de Bolivia: cuatro horas, y el mismo día."""
    converted = to_business_time(datetime(2026, 8, 22, 23, 40, tzinfo=UTC))

    assert converted.strftime("%H:%M") == "19:40"
    assert converted.date().isoformat() == "2026-08-22"


def test_utc_after_midnight_is_still_the_previous_day_in_bolivia() -> None:
    """El caso que corre el día: 01:00 UTC del 23 son las 21:00 del 22 en Bolivia.

    Es la ventana en la que un pago recibido "hoy" caía en el archivo de mañana.
    """
    converted = to_business_time(datetime(2026, 8, 23, 1, 0, tzinfo=UTC))

    assert converted.strftime("%H:%M") == "21:00"
    assert converted.date().isoformat() == "2026-08-22"


def test_a_naive_value_is_read_as_utc_not_as_process_time() -> None:
    """Un valor sin zona viene de la base, donde todo se escribe con `datetime.now(UTC)`.

    Dejarlo al default de `astimezone()` lo interpretaría según **dónde corre el proceso**,
    que es exactamente el error que este módulo viene a arreglar: el mismo dato daría una
    hora distinta en la máquina de quien desarrolla y en el contenedor de producción.
    """
    naive = datetime(2026, 8, 22, 23, 40)

    assert to_business_time(naive).strftime("%H:%M") == "19:40"


def test_an_aware_value_in_another_zone_is_converted_not_relabelled() -> None:
    already_local = datetime(2026, 8, 22, 19, 40, tzinfo=BUSINESS_TZ)

    assert to_business_time(already_local).strftime("%H:%M") == "19:40"


def test_business_today_is_the_bolivian_day() -> None:
    """`business_today` y el día UTC difieren justamente en la ventana de la tarde."""
    assert business_today() == datetime.now(BUSINESS_TZ).date()


def test_receipt_validation_measures_age_against_the_bolivian_day() -> None:
    """El umbral de antigüedad se mide contra el día boliviano.

    La fecha del comprobante la escribe un banco de Bolivia. Con "hoy" corrido, cada tarde
    hay cuatro horas en las que un comprobante del límite cuenta un día de más y se
    rechaza por una fecha que todavía no pasó.
    """
    service = ReceiptValidationService(
        session=MagicMock(), vision=MagicMock(), sender=MagicMock(), publisher=MagicMock()
    )

    assert service._today == business_today()
