"""Corre la extracción de comprobantes contra el modelo **real** y reporta qué leyó (#282).

Cierra la deuda explícita de `docs/FLUJO_PAGO_Y_EVENTOS.md` §11: la suite stubbea el
modelo de visión, así que lo que está probado es **la decisión**, no **la lectura**. Este
script hace lo contrario — usa el mismo `VisionPort`, el mismo prompt y el mismo schema
que el worker, pero contra la API de verdad y contra comprobantes bolivianos reales.

No toca la base ni la cola: lee archivos del disco, llama al proveedor y escribe una
tabla. Se puede correr sin levantar nada.

Uso:

    # 1. Poner los comprobantes (jpg/png/webp/pdf) en una carpeta.
    uv run python scripts/verify_receipt_extraction.py comprobantes/

    # 2. Ver si además auto-aprobarían, con el precio y el beneficiario reales:
    uv run python scripts/verify_receipt_extraction.py comprobantes/ \\
        --precio 650 --beneficiario "MIRKO CALZADILLA"

    # 3. Si hay ground truth (esperado.json en la carpeta), compara campo por campo
    #    y sale con código 1 si algo no coincide — sirve como gate.

Formato de `esperado.json` (opcional, todas las claves opcionales):

    {
      "bnb-01.jpg": {"amount": "650", "paid_at": "2026-08-15",
                     "beneficiary": "MIRKO CALZADILLA", "reference": "2P123456"}
    }

Qué mirar en la salida: **el beneficiario y la referencia son los campos que deciden**.
Si el modelo confunde pagador con destinatario, o toma "Concepto" como identificador, la
validación automática no sirve aunque el monto y la fecha salgan bien.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
from dataclasses import dataclass
from decimal import Decimal
from pathlib import Path

from server.config import get_settings
from server.modules.agent.services.llm_factory import build_vision
from server.modules.crm.domain.money import parse_amount
from server.modules.crm.domain.receipt_checks import ExpectedPayment, evaluate
from server.modules.crm.domain.receipt_extraction import (
    EXTRACTION_INSTRUCTIONS,
    EXTRACTION_SCHEMA,
    describe_for_operator,
    parse_extraction,
)
from server.modules.crm.services.receipt_media import detect_mime
from server.shared.timezone import business_today

FIELDS = ("amount", "currency", "paid_at", "beneficiary", "reference", "bank")
# Los que deciden si un comprobante se puede auto-aprobar. Un fallo acá es bloqueante;
# `bank` y `currency` son contexto.
CRITICAL = ("amount", "paid_at", "beneficiary", "reference")


@dataclass(frozen=True, slots=True)
class FileResult:
    name: str
    read: dict[str, str | None]
    mismatches: tuple[str, ...]
    verdict: str | None
    error: str | None = None


def _load(path: Path) -> tuple[bytes, str] | None:
    """Binario + mime real por magic bytes, igual que el worker. `None` si no sirve."""
    content = path.read_bytes()
    mime = detect_mime(content)
    return (content, mime) if mime is not None else None


def _compare(read: dict[str, str | None], expected: dict[str, str]) -> tuple[str, ...]:
    """Campos donde lo leído no coincide con el ground truth. Vacío = todo bien."""
    off: list[str] = []
    for field, want in expected.items():
        got = read.get(field)
        if field == "amount":
            # Se comparan montos, no strings: "650", "650.00" y "Bs 650" son lo mismo.
            if parse_amount(got) != parse_amount(want):
                off.append(field)
        elif (got or "").strip().casefold() != want.strip().casefold():
            off.append(field)
    return tuple(off)


async def _process(
    path: Path, expected_map: dict[str, dict[str, str]], payment: ExpectedPayment | None
) -> FileResult:
    vision = build_vision(get_settings())
    loaded = _load(path)
    if loaded is None:
        return FileResult(path.name, {}, (), None, error="tipo de archivo no soportado")

    content, mime = loaded
    try:
        raw = await vision.extract(
            content=content,
            mime_type=mime,
            instructions=EXTRACTION_INSTRUCTIONS,
            schema=EXTRACTION_SCHEMA,
        )
    except Exception as exc:  # el proveedor falló: es un resultado del reporte, no un crash
        return FileResult(path.name, {}, (), None, error=f"{type(exc).__name__}: {exc}")

    extracted = parse_extraction(raw)
    read = describe_for_operator(extracted)
    verdict = None
    if payment is not None:
        result = evaluate(extracted, payment, today=business_today())
        verdict = (
            "AUTO-APRUEBA" if result.passed else "a humano (" + ", ".join(result.failed_codes) + ")"
        )
    return FileResult(path.name, read, _compare(read, expected_map.get(path.name, {})), verdict)


def _print(result: FileResult) -> None:
    print(f"\n-- {result.name}")
    if result.error is not None:
        print(f"   ERROR: {result.error}")
        return
    for field in FIELDS:
        flag = "  <-- NO COINCIDE" if field in result.mismatches else ""
        print(f"   {field:<12} {result.read.get(field) or '-'}{flag}")
    if result.verdict is not None:
        print(f"   {'veredicto':<12} {result.verdict}")


def _summary(results: list[FileResult]) -> int:
    """Tasa de acierto por campo crítico. Devuelve el exit code."""
    checked = [r for r in results if r.error is None]
    failed = [r for r in results if r.error is not None]
    blocking = [r for r in checked if set(r.mismatches) & set(CRITICAL)]

    print(f"\n{'=' * 60}\n{len(checked)} comprobantes leídos, {len(failed)} con error")
    for field in CRITICAL:
        filled = sum(1 for r in checked if r.read.get(field))
        wrong = sum(1 for r in checked if field in r.mismatches)
        print(
            f"  {field:<12} leído en {filled}/{len(checked)}" + (f" · {wrong} mal" if wrong else "")
        )
    if blocking:
        print(
            f"\n[!] {len(blocking)} con un campo critico mal: "
            + ", ".join(r.name for r in blocking)
        )
    return 1 if blocking or failed else 0


async def run(folder: Path, precio: str | None, beneficiario: str | None) -> int:
    settings = get_settings()
    model = (
        settings.llm_openai_model_vision
        if settings.llm_provider == "openai"
        else settings.llm_model_vision
    )
    print(f"Proveedor: {settings.llm_provider} · modelo de visión: {model}")

    expected_file = folder / "esperado.json"
    expected_map: dict[str, dict[str, str]] = (
        json.loads(expected_file.read_text(encoding="utf-8")) if expected_file.is_file() else {}
    )
    if expected_map:
        print(f"Ground truth: {expected_file.name} ({len(expected_map)} archivos)")

    payment: ExpectedPayment | None = None
    if precio is not None:
        amount: Decimal | None = parse_amount(precio)
        payment = ExpectedPayment(amount=amount, currency="BOB", beneficiary=beneficiario)
        print(
            f"Comparando contra: precio {amount} BOB · beneficiario {beneficiario or '(sin cargar)'}"
        )

    files = sorted(p for p in folder.iterdir() if p.is_file() and p.name != "esperado.json")
    if not files:
        print(f"No hay archivos en {folder}")
        return 1

    results = [await _process(path, expected_map, payment) for path in files]
    for result in results:
        _print(result)
    return _summary(results)


def main() -> None:
    parser = argparse.ArgumentParser(description="Verifica la extracción contra el modelo real.")
    parser.add_argument("carpeta", type=Path, help="Carpeta con los comprobantes.")
    parser.add_argument("--precio", help="Precio esperado, para simular los checks.")
    parser.add_argument("--beneficiario", help="Beneficiario esperado, para simular los checks.")
    args = parser.parse_args()

    if not args.carpeta.is_dir():
        print(f"No existe la carpeta {args.carpeta}")
        sys.exit(1)
    sys.exit(asyncio.run(run(args.carpeta, args.precio, args.beneficiario)))


if __name__ == "__main__":
    main()
