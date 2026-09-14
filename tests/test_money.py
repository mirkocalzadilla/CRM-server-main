"""Normalización de montos (`crm/domain/money.py`) — lógica pura, sin DB.

Los casos vienen de los comprobantes reales del Anexo A del handoff de pago/visión
(ZAS, BNB, Ganadero, Mercantil) y de los precios cargados en el catálogo. Cubre las
tres convenciones que conviven y, sobre todo, que un precio ambiguo devuelva `None`
en vez de un número inventado con el que después se validaría un pago.
"""

from __future__ import annotations

from decimal import Decimal

import pytest

from server.modules.crm.domain.money import parse_amount


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        # Comprobantes reales (Anexo A): coma = miles, punto = decimal.
        ("Bs 10,000.00", Decimal("10000.00")),
        ("Bs 65,200.00", Decimal("65200.00")),
        ("Bs46.00", Decimal("46.00")),  # pegado al símbolo
        ("122.5", Decimal("122.50")),  # punto decimal de 1 dígito
        ("220", Decimal("220.00")),  # pelado
        ("6000", Decimal("6000.00")),  # pelado, sin separadores
        ("14.00", Decimal("14.00")),
        ("555.00", Decimal("555.00")),
        ("Bs 30.00", Decimal("30.00")),
        ("233", Decimal("233.00")),
        # Precios del catálogo: punto = miles (convención boliviana de escritura).
        ("1.800", Decimal("1800.00")),
        ("3.800 Bs", Decimal("3800.00")),
        ("10.000", Decimal("10000.00")),
        ("2.500", Decimal("2500.00")),
        # Coma como decimal (escritura europea/local alternativa).
        ("122,5", Decimal("122.50")),
        ("10,000", Decimal("10000.00")),  # coma de miles: 3 dígitos detrás
        # Ruido alrededor del número.
        ("  480 Bs  ", Decimal("480.00")),
        ("Monto: Bs 1.500", Decimal("1500.00")),
    ],
)
def test_parses_unambiguous_amounts(raw: str, expected: Decimal) -> None:
    assert parse_amount(raw) == expected


@pytest.mark.parametrize(
    "raw",
    [
        None,
        "",
        "   ",
        "sin referencia",  # texto sin número
        "1.800 / 3.000 Bs",  # rango con dos números
        "desde 3.800",  # rango abierto
        "hasta 5.000",
        "a partir de 1.200",
        "entre 500 y 900",
        "1.800 - 3.000",
        "500 o 900",
        "aprox 700",
        "1.500 + IVA",
        "0",  # no positivo
        "999999999",  # excede el máximo permitido
        "1.2345",  # separador que no es ni miles ni decimales
        "1.80.00",  # grupos de miles inválidos
    ],
)
def test_rejects_ambiguous_or_invalid(raw: str | None) -> None:
    assert parse_amount(raw) is None


def test_leading_minus_is_read_as_range_marker() -> None:
    """ "-40" no es un precio: el guion se trata como marca de rango, no como signo.

    Ni el catálogo ni un comprobante muestran montos negativos; lo que importa es
    que no se convierta silenciosamente en un monto válido."""
    assert parse_amount("-40") is None


def test_amount_is_always_two_decimals() -> None:
    assert str(parse_amount("480")) == "480.00"
    assert str(parse_amount("122.5")) == "122.50"


def test_max_amount_boundary() -> None:
    assert parse_amount("100000") == Decimal("100000.00")
    assert parse_amount("100000.01") is None
