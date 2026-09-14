"""Resolución del título visible de un lead.

Precedencia: nombre de la conversación (editable desde el detalle) → nombre del
contacto vinculado → teléfono. Se resuelve en la proyección (board/detalle) para
que renombrar un contacto se refleje en todas sus cards sin migrar datos.
"""

from __future__ import annotations


def resolve_lead_title(*candidates: str | None) -> str:
    """First non-empty candidate; empty string if none."""
    for candidate in candidates:
        if candidate:
            return candidate
    return ""
