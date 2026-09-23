"""Keyword detection for opt-out requests (Meta marketing policy: honour STOP)."""

from __future__ import annotations

import re
import unicodedata

_KEYWORDS = {
    "baja",
    "stop",
    "cancelar",
    "no quiero recibir mas mensajes",
    "no me escriban mas",
    "no me escribas mas",
    "dejen de escribirme",
    "deja de escribirme",
    "no mas mensajes",
    "unsubscribe",
}

OPT_OUT_REPLY = (
    "Listo, no te vamos a escribir más. Si algún día querés retomar, "
    "escribinos por acá y con gusto te ayudamos."
)


def _normalize(text: str) -> str:
    stripped = unicodedata.normalize("NFKD", text)
    ascii_only = "".join(ch for ch in stripped if not unicodedata.combining(ch))
    return re.sub(r"[^a-z0-9 ]+", " ", ascii_only.lower()).strip()


def is_opt_out_request(text: str) -> bool:
    normalized = re.sub(r"\s+", " ", _normalize(text))
    return normalized in _KEYWORDS
