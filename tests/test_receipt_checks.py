"""Checks del comprobante y matching de beneficiario — lógica pura, sin DB ni LLM.

Los casos vienen de los comprobantes reales del Anexo A del handoff. Lo que fijan estos
tests es el **sesgo del diseño**: la duda va a un humano. Un dato ilegible, una moneda
que no corresponde, un precio en rango — nada de eso se aprueba solo.

Cubre los casos 3, 4, 5, 6, 9, 11 y 13 de la matriz del handoff a nivel de decisión (el
circuito completo con DB está en el test del servicio de visión).
"""

from __future__ import annotations

from datetime import date
from decimal import Decimal

import pytest

from server.modules.crm.domain.beneficiary import matches_beneficiary, normalize_name
from server.modules.crm.domain.receipt_checks import (
    CHECK_AMOUNT,
    CHECK_BENEFICIARY,
    CHECK_CURRENCY,
    CHECK_DATE,
    CHECK_PRICE,
    CHECK_REFERENCE,
    ExpectedPayment,
    ExtractedReceipt,
    evaluate,
)
from server.modules.crm.domain.receipt_extraction import parse_extraction

TODAY = date(2026, 8, 23)
MIRKO = "Mirko Calzadilla"


def _expected(amount: str | None = "650.00", currency: str = "BOB") -> ExpectedPayment:
    return ExpectedPayment(
        amount=Decimal(amount) if amount is not None else None,
        currency=currency,
        beneficiary=MIRKO,
    )


def _extracted(**over: object) -> ExtractedReceipt:
    base: dict[str, object] = {
        "amount": Decimal("650.00"),
        "currency": "BOB",
        "paid_at": date(2026, 8, 22),
        "beneficiary": "MIRKO CALZADILLA",
        "reference": "2P10019819",
    }
    base.update(over)
    return ExtractedReceipt(**base)  # type: ignore[arg-type]


# --------------------------- happy path ---------------------------


def test_valid_receipt_passes() -> None:
    verdict = evaluate(_extracted(), _expected(), today=TODAY)
    assert verdict.passed is True
    assert verdict.failed_codes == ()


def test_all_checks_run_even_when_one_fails() -> None:
    """El operador necesita el cuadro completo, no el primer problema."""
    verdict = evaluate(
        _extracted(amount=Decimal("10.00"), beneficiary="Otra Persona", reference=None),
        _expected(),
        today=TODAY,
    )
    assert set(verdict.failed_codes) == {CHECK_AMOUNT, CHECK_BENEFICIARY, CHECK_REFERENCE}
    assert len(verdict.checks) == 6  # se corrieron todos


# --------------------------- caso 3: monto distinto ---------------------------


@pytest.mark.parametrize("paid", ["600.00", "700.00", "65.00", "6500.00"])
def test_wrong_amount_fails(paid: str) -> None:
    verdict = evaluate(_extracted(amount=Decimal(paid)), _expected("650.00"), today=TODAY)
    assert verdict.passed is False
    assert CHECK_AMOUNT in verdict.failed_codes


def test_amount_must_be_exact_no_tolerance() -> None:
    """Un sobrepago de un centavo es exactamente lo que un humano tiene que mirar."""
    verdict = evaluate(_extracted(amount=Decimal("650.01")), _expected("650.00"), today=TODAY)
    assert CHECK_AMOUNT in verdict.failed_codes


def test_unreadable_amount_fails() -> None:
    verdict = evaluate(_extracted(amount=None), _expected(), today=TODAY)
    assert CHECK_AMOUNT in verdict.failed_codes


# --------------------------- caso 4 y 5: beneficiario ---------------------------


def test_wrong_beneficiary_fails() -> None:
    verdict = evaluate(_extracted(beneficiary="DENNY OLIVA ALVAREZ"), _expected(), today=TODAY)
    assert verdict.passed is False
    assert CHECK_BENEFICIARY in verdict.failed_codes


@pytest.mark.parametrize(
    "found",
    [
        "MIRKO CALZADILLA",  # mayúsculas
        "Mirko Calzadilla",  # exacto
        "CALZADILLA MIRKO",  # orden invertido (Ganadero escribe apellido primero)
        "CALZADILLA VARGAS MIRKO ANDRES",  # con más nombres de los configurados
        "MIRKO CALZADIL",  # truncado por el banco (caso real del BNB)
        "Mirko Calzadílla",  # con tilde espuria
        "M*** C***",  # enmascarado
        "  mirko   calzadilla  ",  # espacios de más
        "MIRKO DE CALZADILLA",  # partícula que un layout agrega
    ],
)
def test_tolerant_beneficiary_matching(found: str) -> None:
    verdict = evaluate(_extracted(beneficiary=found), _expected(), today=TODAY)
    assert verdict.passed is True, f"{found} debería matchear"


@pytest.mark.parametrize(
    "found",
    [
        "MIRKO GUTIERREZ",  # mismo nombre, otro apellido
        "JACKELINE CUELLAR SERRUDO",
        "M",  # una inicial sola no identifica a nadie
        "",
        "   ",
    ],
)
def test_beneficiary_that_should_not_match(found: str) -> None:
    verdict = evaluate(_extracted(beneficiary=found), _expected(), today=TODAY)
    assert CHECK_BENEFICIARY in verdict.failed_codes, f"{found} NO debería matchear"


@pytest.mark.parametrize(
    "found",
    [
        "M*** G***",  # iniciales que no son las esperadas
        "J*** C***",
        "M***",  # una sola palabra para un nombre de dos
        "M*** C*** V***",  # más palabras que el nombre esperado
    ],
)
def test_masked_beneficiary_is_not_a_free_pass(found: str) -> None:
    """La tolerancia a máscaras exige la misma cantidad de palabras y las mismas
    iniciales: es una señal débil, pero no un colador."""
    verdict = evaluate(_extracted(beneficiary=found), _expected(), today=TODAY)
    assert CHECK_BENEFICIARY in verdict.failed_codes, f"{found} NO debería matchear"


def test_masked_beneficiary_tolerates_reversed_order() -> None:
    """Unos layouts enmascaran apellido primero; con solo iniciales no se puede saber
    cuál es cuál, así que se compara el conjunto."""
    verdict = evaluate(_extracted(beneficiary="C*** M***"), _expected(), today=TODAY)
    assert verdict.passed is True


def test_unconfigured_beneficiary_never_passes() -> None:
    """Sin beneficiario esperado no se puede afirmar que el pago entró a la cuenta
    correcta: falla segura (el estado en que queda una organización recién migrada)."""
    expected = ExpectedPayment(amount=Decimal("650.00"), currency="BOB", beneficiary=None)
    verdict = evaluate(_extracted(), expected, today=TODAY)
    assert verdict.passed is False
    assert CHECK_BENEFICIARY in verdict.failed_codes


# --------------------------- caso 6: antigüedad ---------------------------


def test_receipt_within_window_passes() -> None:
    verdict = evaluate(_extracted(paid_at=date(2026, 8, 16)), _expected(), today=TODAY)
    assert verdict.passed is True  # exactamente 7 días


def test_receipt_too_old_fails() -> None:
    verdict = evaluate(_extracted(paid_at=date(2026, 8, 15)), _expected(), today=TODAY)
    assert verdict.passed is False
    assert CHECK_DATE in verdict.failed_codes


def test_receipt_dated_in_the_future_fails() -> None:
    """Una fecha futura es señal de edición o de un reloj mal puesto, no de un pago."""
    verdict = evaluate(_extracted(paid_at=date(2026, 8, 24)), _expected(), today=TODAY)
    assert CHECK_DATE in verdict.failed_codes


def test_unreadable_date_fails() -> None:
    verdict = evaluate(_extracted(paid_at=None), _expected(), today=TODAY)
    assert CHECK_DATE in verdict.failed_codes


def test_date_check_uses_injected_clock_not_the_real_one() -> None:
    """Los fixtures reales envejecen: el check se prueba con reloj inyectado, así que
    estos tests siguen valiendo el año que viene."""
    old_receipt = _extracted(paid_at=date(2026, 8, 6))
    assert evaluate(old_receipt, _expected(), today=date(2026, 8, 8)).passed is True
    assert CHECK_DATE in evaluate(old_receipt, _expected(), today=date(2026, 9, 1)).failed_codes


# --------------------------- caso 9: sin referencia ---------------------------


@pytest.mark.parametrize("reference", [None, "", "   "])
def test_missing_reference_never_passes(reference: str | None) -> None:
    """Sin identificador no se puede detectar el reuso del mismo comprobante."""
    verdict = evaluate(_extracted(reference=reference), _expected(), today=TODAY)
    assert verdict.passed is False
    assert CHECK_REFERENCE in verdict.failed_codes


# --------------------------- caso 11: comprobante ilegible ---------------------------


def test_unreadable_image_fails_on_missing_fields() -> None:
    """Un meme o una foto borrosa: no hay campos, no hay aprobación."""
    verdict = evaluate(ExtractedReceipt(), _expected(), today=TODAY)
    assert verdict.passed is False
    assert set(verdict.failed_codes) >= {
        CHECK_AMOUNT,
        CHECK_BENEFICIARY,
        CHECK_DATE,
        CHECK_REFERENCE,
    }


# --------------------------- caso 13: USD y precio en rango ---------------------------


def test_usd_service_never_auto_passes() -> None:
    """No hay tasa de cambio en el sistema: el monto en Bs lo confirma el equipo."""
    verdict = evaluate(_extracted(), _expected("1200.00", currency="USD"), today=TODAY)
    assert verdict.passed is False
    assert CHECK_CURRENCY in verdict.failed_codes


def test_service_without_numeric_price_never_auto_passes() -> None:
    """Precio en rango ("1.800 / 3.000"): cualquiera de los dos montos sería correcto."""
    verdict = evaluate(_extracted(), _expected(None), today=TODAY)
    assert verdict.passed is False
    assert CHECK_PRICE in verdict.failed_codes
    assert CHECK_AMOUNT in verdict.failed_codes


def test_receipt_in_another_currency_fails() -> None:
    verdict = evaluate(_extracted(currency="USD"), _expected(), today=TODAY)
    assert CHECK_CURRENCY in verdict.failed_codes


# --------------------------- normalización de nombres ---------------------------


def test_normalize_name_drops_noise_and_accents() -> None:
    assert normalize_name("Durán de la Oliva") == ["duran", "oliva"]
    assert normalize_name("M*** C***") == ["m", "c"]
    assert normalize_name(None) == []
    assert normalize_name("") == []


def test_matches_beneficiary_needs_both_sides() -> None:
    assert matches_beneficiary(None, "Mirko Calzadilla") is False
    assert matches_beneficiary("Mirko Calzadilla", None) is False
    assert matches_beneficiary("", "") is False


def test_placeholder_reference_never_satisfies_the_reuse_defence() -> None:
    """`<UNKNOWN>` es "no lo pude leer", no un identificador (#282).

    Salió de la corrida real contra los comprobantes de producción: el modelo devolvió
    el marcador pese a que el prompt pide dejar el campo vacío. Como string no vacío
    hacía pasar el check de referencia —la única defensa anti-reuso— y además ocupaba
    el UNIQUE `(organization_id, reference)`, con lo que el siguiente comprobante
    ilegible se rechazaba como "ya usado".
    """
    extracted = parse_extraction(
        {
            "amount": "450.00",
            "currency": "BOB",
            "paid_at": None,
            "beneficiary": "<UNKNOWN>",
            "reference": "<UNKNOWN>",
            "bank": None,
        }
    )
    assert extracted.reference is None
    assert extracted.beneficiary is None

    verdict = evaluate(
        extracted,
        ExpectedPayment(
            amount=Decimal("450.00"), currency="BOB", beneficiary="Carmelo Mirko Calzadilla Cuellar"
        ),
        today=date(2026, 8, 28),
    )
    assert not verdict.passed
    assert CHECK_REFERENCE in verdict.failed_codes


@pytest.mark.parametrize(
    "marker", ["UNKNOWN", "<unknown>", "N/A", "null", "-", "Desconocido", "no disponible", "[N/A]"]
)
def test_placeholder_markers_read_as_missing(marker: str) -> None:
    assert parse_extraction({"reference": marker}).reference is None


def test_a_real_reference_that_looks_like_a_word_survives() -> None:
    """El filtro no puede comerse un identificador legítimo."""
    assert parse_extraction({"reference": "2P53976940"}).reference == "2P53976940"
    assert parse_extraction({"beneficiary": "Ana Nunez"}).beneficiary == "Ana Nunez"
