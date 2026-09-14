"""The validation note the operator reads in the conversation's AI summary.

When the automatic validation cannot approve a receipt, the full reason lives in the
receipt panel — but the operator's first read is `conversation.ai_summary` (the inbox
and the opportunity detail show it), and until now it only said *that* the lead sent a
receipt, not *why* nothing happened next (UAT 2026-08-31). This module rewrites that
summary with a deterministic note naming what failed, reusing the check `detail`
strings the panel already shows — no LLM call.

Pure text logic. The marker makes the note replaceable: a second failure swaps the old
note instead of stacking, and an approval removes it instead of leaving it stale.
"""

from __future__ import annotations

from server.modules.crm.domain.receipt_checks import ReceiptVerdict

NOTE_MARKER = "⚠️ Validación automática:"


def with_validation_note(summary: str | None, verdict: ReceiptVerdict) -> str:
    """The summary with this verdict's note, replacing any previous note."""
    reasons = "; ".join(check.detail for check in verdict.checks if not check.passed)
    note = (
        f"{NOTE_MARKER} el comprobante no se pudo aprobar solo — {reasons}. "
        "Quedó para revisión humana: validalo o rechazalo desde el panel de pago de la card."
    )
    base = without_validation_note(summary)
    return f"{base}\n\n{note}" if base else note


def without_validation_note(summary: str | None) -> str | None:
    """The summary without the validation note (a fresh approval outdates it)."""
    if not summary or NOTE_MARKER not in summary:
        return summary
    base = summary.split(NOTE_MARKER, 1)[0].rstrip()
    return base or None
