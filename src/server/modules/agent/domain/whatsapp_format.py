"""Outbound text normalization for WhatsApp (safety net for B2/A8).

WhatsApp does not render markdown: it shows `**bold**` and `# Title` literally. The
persona prompt already tells the agent to avoid markdown; this is the belt-and-suspenders
pass on the way out, so a stray `**` never reaches the customer as raw asterisks.
WhatsApp's own markup (single `*bold*`, `_italic_`) is kept.

It also drops the Spanish opening marks `¡`/`¿`: the Bolivian-informal persona forbids
them (A8), but the model still leaks them under load, so we strip them deterministically.
"""

from __future__ import annotations

import re

# `**x**` / `__x__` → WhatsApp-native `*x*` / `_x_`; line-leading `#` headings dropped.
_MD_BOLD = re.compile(r"\*\*(.+?)\*\*", re.DOTALL)
_MD_UNDERSCORE_BOLD = re.compile(r"__(.+?)__", re.DOTALL)
_MD_HEADING = re.compile(r"^[ \t]{0,3}#{1,6}[ \t]+", re.MULTILINE)


def to_whatsapp_text(body: str) -> str:
    """Normalize markdown the model may emit into text WhatsApp renders correctly,
    and drop the opening marks ¡/¿ the persona forbids (A8)."""
    out = _MD_BOLD.sub(r"*\1*", body)
    out = _MD_UNDERSCORE_BOLD.sub(r"_\1_", out)
    out = _MD_HEADING.sub("", out)
    out = out.replace("¡", "").replace("¿", "")
    return out
