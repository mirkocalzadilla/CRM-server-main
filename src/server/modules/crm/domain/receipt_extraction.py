"""Qué se le pide al modelo de visión, y cómo se interpreta lo que devuelve.

El prompt y el schema están acá, junto al parseo, porque son una sola decisión: cada
instrucción existe por un formato concreto que aparece en los comprobantes bolivianos
reales, y el parseo tiene que aceptar exactamente eso.

Los aprendizajes que codifica (relevados de comprobantes de ZAS, BNB, Banco Ganadero y
Mercantil Santa Cruz):

1. **El identificador de la transacción se llama distinto en cada banco**: "Nro. de
   transacción" (ZAS), "Bancarización" (BNB, con formato `2P…`), "Nro." (Ganadero),
   "Código de transacción" (Mercantil, 19 dígitos). Y lo más importante:
   **"Referencia", "Nota" y "Concepto" NO son el identificador** — son texto que
   escribe quien paga, y pueden traer datos de terceros que engañan ("pasanaco",
   "Almuerzo", o el nombre y la cuenta de otra persona).
2. **Los montos mezclan convenciones**: "Bs 10,000.00" (coma de miles), "Bs46.00"
   (pegado al símbolo), "122.5" (un decimal), "6000" (pelado). Se pide el número tal
   como está escrito y lo normaliza el código, que sabe la regla.
3. **Las fechas también**: "06/Ago/2026 17:29:10", "09/08/2026" con la hora en otro
   campo, "15 de Agosto, 2026 a las 21:12". Se pide ISO.
4. **Hay dos nombres en todo comprobante** (quien paga y quien recibe) y confundirlos
   invierte el sentido de la validación. Se pide explícitamente el del destinatario.
"""

from __future__ import annotations

import re
from datetime import date
from decimal import Decimal
from typing import Any

from server.modules.crm.domain.money import parse_amount
from server.modules.crm.domain.receipt_checks import ExtractedReceipt

EXTRACTION_INSTRUCTIONS = """Este es un comprobante de una transferencia o un pago QR de un banco boliviano.

Leé y registrá estos datos, exactamente como figuran en la imagen:

- amount: el monto de la operación, tal como está escrito (por ejemplo "Bs 10,000.00", "Bs46.00", "122.5", "6000"). No lo reformatees ni le quites separadores.
- currency: la moneda: "BOB" si dice Bs o Bolivianos, "USD" si dice $ o USD.
- paid_at: la fecha de la operación en formato ISO (YYYY-MM-DD). Los comprobantes la escriben de varias formas ("06/Ago/2026", "09/08/2026", "15 de Agosto, 2026"): son día/mes/año.
- beneficiary: el nombre de QUIEN RECIBE el dinero (los campos suelen llamarse "Beneficiario", "Nombre del destinatario", "Cuenta de destino" o "Cuenta destino"). NO el de quien paga (que aparece como "Pagador", "Nombre del originante", "Cuenta de origen").
- reference: el identificador único de la transacción. Según el banco el campo se llama "Nro. de transacción", "Bancarización" (suele empezar con 2P), "Nro.", "Código de transacción" o "Código de autorización". IMPORTANTE: los campos "Referencia", "Nota" y "Concepto" NO son el identificador — son texto libre que escribió quien paga y a veces traen datos de otra persona. Si el identificador no está o no se lee, dejá reference vacío.
- bank: el banco que recibe el dinero, si figura.

Un campo que no puedas leer con seguridad dejalo vacío. **No adivines ni completes por contexto**: un dato vacío es una respuesta correcta y útil, un dato inventado hace que se apruebe un pago que no entró."""

EXTRACTION_SCHEMA: dict[str, object] = {
    "type": "object",
    "properties": {
        "amount": {
            "type": ["string", "null"],
            "description": "Monto tal como está escrito en el comprobante.",
        },
        "currency": {
            "type": ["string", "null"],
            "enum": ["BOB", "USD", None],
            "description": "Moneda de la operación.",
        },
        "paid_at": {
            "type": ["string", "null"],
            "description": "Fecha de la operación en formato ISO YYYY-MM-DD.",
        },
        "beneficiary": {
            "type": ["string", "null"],
            "description": "Nombre de quien RECIBE el dinero.",
        },
        "reference": {
            "type": ["string", "null"],
            "description": "Identificador único de la transacción (no la referencia libre).",
        },
        "bank": {"type": ["string", "null"], "description": "Banco destino."},
    },
    "required": ["amount", "currency", "paid_at", "beneficiary", "reference"],
}


# Marcadores de "no lo pude leer" que el modelo escribe en vez de dejar el campo vacío.
# Se comparan sin los adornos (`<`, `>`, `[`, `]`, `.`) y sin distinguir mayúsculas.
_PLACEHOLDERS: frozenset[str] = frozenset(
    {
        "unknown",
        "desconocido",
        "n/a",
        "na",
        "null",
        "none",
        "nan",
        "-",
        "--",
        "?",
        "sin dato",
        "sin datos",
        "no disponible",
        "no especificado",
        "not available",
        "not found",
    }
)
_PLACEHOLDER_CHARS = re.compile(r"[<>\[\]().]")


def parse_extraction(raw: dict[str, object]) -> ExtractedReceipt:
    """Convierte la respuesta del modelo en datos tipados.

    Tolerante por diseño: cualquier campo que no se pueda interpretar queda en `None`,
    que los checks leen como "no se pudo leer" y derivan a un humano. Nunca levanta:
    una respuesta rara del modelo no puede tumbar el worker.
    """
    return ExtractedReceipt(
        amount=_amount(raw.get("amount")),
        currency=_currency(raw.get("currency")),
        paid_at=_date(raw.get("paid_at")),
        beneficiary=_text(raw.get("beneficiary")),
        reference=_text(raw.get("reference")),
        bank=_text(raw.get("bank")),
    )


def _amount(value: object) -> Decimal | None:
    if isinstance(value, int | float) and not isinstance(value, bool):
        # Algunos modelos devuelven el número ya parseado pese a pedirse texto.
        return parse_amount(str(value))
    return parse_amount(value) if isinstance(value, str) else None


def _currency(value: object) -> str | None:
    if not isinstance(value, str):
        return None
    cleaned = value.strip().upper()
    return cleaned if cleaned in ("BOB", "USD") else None


def _date(value: object) -> date | None:
    if not isinstance(value, str):
        return None
    # Se pidió ISO; se acepta con hora ("2026-08-22T19:32:54") por si la agrega.
    text = value.strip().replace("/", "-")
    if not text:
        return None
    try:
        return date.fromisoformat(text[:10])
    except ValueError:
        return None


def _text(value: object) -> str | None:
    if not isinstance(value, str):
        return None
    cleaned = " ".join(value.split())
    if not cleaned or _is_placeholder(cleaned):
        return None
    return cleaned


def _is_placeholder(text: str) -> bool:
    """Whether the model wrote a "couldn't read it" marker instead of leaving it empty.

    Verificado contra comprobantes reales (#282): pese a que el prompt pide dejar vacío
    lo que no se pueda leer, el modelo a veces devuelve `<UNKNOWN>`. Eso viajaba como si
    fuera un dato: el check de referencia lo daba por bueno —anulando la única defensa
    anti-reuso— y el valor ocupaba el UNIQUE `(organization_id, reference)`, así que el
    siguiente comprobante ilegible se rechazaba como "ya usado" contra un lead inocente.
    """
    return _PLACEHOLDER_CHARS.sub("", text).strip().casefold() in _PLACEHOLDERS


def describe_for_operator(extracted: ExtractedReceipt) -> dict[str, Any]:
    """Datos extraídos en forma serializable, para guardarlos y mostrarlos en el CRM."""
    return {
        "amount": str(extracted.amount) if extracted.amount is not None else None,
        "currency": extracted.currency,
        "paid_at": extracted.paid_at.isoformat() if extracted.paid_at is not None else None,
        "beneficiary": extracted.beneficiary,
        "reference": extracted.reference,
        "bank": extracted.bank,
    }
