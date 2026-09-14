"""La nota de revisión del resumen IA (`crm/domain/receipt_summary.py`) — lógica pura.

El circuito completo (fallo escribe, aprobación borra, el segundo fallo reemplaza) se
prueba en `test_receipt_validation.py`; acá van los bordes del texto.
"""

from __future__ import annotations

from server.modules.crm.domain.receipt_checks import (
    VERDICT_FAIL,
    CheckResult,
    ReceiptVerdict,
)
from server.modules.crm.domain.receipt_summary import (
    NOTE_MARKER,
    with_validation_note,
    without_validation_note,
)

_VERDICT = ReceiptVerdict(
    verdict=VERDICT_FAIL,
    checks=(
        CheckResult("amount", False, "el comprobante dice 600 y el servicio cuesta 650"),
        CheckResult("date", True, "comprobante del 2026-08-30"),
        CheckResult("reference", False, "no se pudo leer el número de transacción"),
    ),
)


def test_note_names_only_the_failed_checks() -> None:
    note = with_validation_note(None, _VERDICT)
    assert note.startswith(NOTE_MARKER)
    assert "600" in note
    assert "número de transacción" in note
    assert "2026-08-30" not in note  # los checks en verde no son algo a subsanar


def test_note_appends_to_an_existing_summary() -> None:
    result = with_validation_note("Resumen del handoff.", _VERDICT)
    assert result.startswith("Resumen del handoff.")
    assert NOTE_MARKER in result


def test_replacing_never_stacks_notes() -> None:
    once = with_validation_note("Resumen.", _VERDICT)
    twice = with_validation_note(once, _VERDICT)
    assert twice.count(NOTE_MARKER) == 1
    assert twice.startswith("Resumen.")


def test_strip_restores_the_original_summary() -> None:
    assert without_validation_note(with_validation_note("Resumen.", _VERDICT)) == "Resumen."


def test_strip_of_a_note_only_summary_is_none() -> None:
    assert without_validation_note(with_validation_note(None, _VERDICT)) is None


def test_strip_without_a_note_is_identity() -> None:
    assert without_validation_note("Resumen.") == "Resumen."
    assert without_validation_note(None) is None
    assert without_validation_note("") == ""
