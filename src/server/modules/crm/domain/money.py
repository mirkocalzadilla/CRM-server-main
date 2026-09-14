"""Normalización de montos escritos por humanos o leídos de un comprobante.

Función pura, sin DB ni IO: la comparten el backfill del catálogo (texto display →
`price_amount`) y el check de monto de la validación automática de pagos.

Los layouts bolivianos mezclan convenciones dentro de un mismo circuito: "Bs
10,000.00" y "Bs 65,200.00" (coma = miles, punto = decimal), "1.800" (punto = miles),
"122.5" (punto = decimal), "Bs46.00" (pegado al símbolo) y "6000" (pelado). El
criterio es el **número de dígitos que siguen al último separador**: 3 ⇒ es de miles,
1 o 2 ⇒ es decimal.

Ante ambigüedad real (rango "1.800 / 3.000", "desde 3.800", más de un número) se
devuelve `None`: el caller lo trata como dato faltante y deriva a un humano. Nunca
se adivina un monto con el que después se valida un pago.
"""

from __future__ import annotations

import re
from decimal import Decimal, InvalidOperation

# Un número con sus separadores internos: 6000 · 122.5 · 1.800 · 10,000.00 · 65.200,50
_NUMBER = re.compile(r"\d+(?:[.,]\d+)*")
# Palabras que delatan un precio no puntual ("desde 3.800", "a partir de", "hasta").
_RANGE_WORDS = re.compile(r"desde|hasta|a\s*partir|aprox|entre|/|-|\bo\b|\+", re.IGNORECASE)
MAX_AMOUNT = Decimal("100000.00")


def parse_amount(raw: str | None) -> Decimal | None:
    """Monto canónico (2 decimales) o `None` si el texto no lo determina sin ambigüedad.

    `None` cuando: está vacío, no hay número, hay más de un número, aparece una
    palabra de rango, el valor no es positivo o excede `MAX_AMOUNT`.
    """
    if raw is None:
        return None
    text = raw.strip()
    if not text:
        return None

    numbers = _NUMBER.findall(text)
    if len(numbers) != 1:
        return None  # ningún número, o un rango del tipo "1.800 / 3.000"
    if _RANGE_WORDS.search(text):
        return None  # "desde 3.800", "1.800 - 3.000": no es un precio puntual

    try:
        amount = _to_decimal(numbers[0])
    except (InvalidOperation, ValueError):
        return None
    if amount is None or amount <= 0 or amount > MAX_AMOUNT:
        return None
    return amount.quantize(Decimal("0.01"))


def _to_decimal(number: str) -> Decimal | None:
    """Resuelve los separadores de `number` a un Decimal.

    Con dos tipos de separador, el último es el decimal y el otro es de miles. Con
    uno solo, decide por la cantidad de dígitos que lo siguen: 3 ⇒ miles ("1.800" =
    1800), 1 o 2 ⇒ decimal ("122.5" = 122.50). Repetido ("1.234.567") ⇒ siempre miles.
    """
    if "." in number and "," in number:
        decimal_sep = "." if number.rfind(".") > number.rfind(",") else ","
        thousands_sep = "," if decimal_sep == "." else "."
        return Decimal(number.replace(thousands_sep, "").replace(decimal_sep, "."))

    separator = "." if "." in number else ("," if "," in number else "")
    if not separator:
        return Decimal(number)

    groups = number.split(separator)
    tail = groups[-1]
    if len(groups) > 2 or len(tail) == 3:
        # 1.234.567 / 10,000 / 1.800 → separador de miles. Los grupos intermedios
        # deben ser de 3 dígitos; si no, el texto no es un número confiable.
        if any(len(group) != 3 for group in groups[1:]):
            return None
        return Decimal("".join(groups))
    if len(tail) in (1, 2):
        return Decimal(f"{groups[0]}.{tail}")
    return None  # "1.2345": ni miles ni decimales válidos
