"""Comparación tolerante de nombres de beneficiario.

El nombre del destinatario de un comprobante nunca llega igual a como está cargado en
la config: cada banco lo escribe distinto. De los comprobantes reales relevados:

- **Orden invertido**: "DURAN OLIVA NATALIA BOLIVIA" vs "Natalia Duran".
- **Truncamiento**: el BNB corta el campo ("NATALIA BOLIVIA DURA" por "…DURAN").
- **Mayúsculas y tildes**: "JONHNY DONALD DURAN" / "Jonhny Donald Durán".
- **Nombres parciales**: falta el segundo apellido ("JONHNY DONALD DURAN" por
  "JONHNY DONALD DURAN JUSTINIANO").
- **Máscaras**: algunos formatos ocultan letras ("M*** C***").

La estrategia es comparar **conjuntos de tokens**, no cadenas: el nombre esperado
coincide si cada una de sus palabras aparece en el comprobante (permitiendo prefijos,
para el truncamiento y las máscaras). Es tolerante en la dirección segura — un nombre
distinto no pasa — y cuando no está seguro devuelve `False`, que manda el comprobante
a revisión humana en vez de aprobarlo.
"""

from __future__ import annotations

import re
import unicodedata

# Partículas que no aportan identidad y que los bancos incluyen o no según el layout.
_NOISE: frozenset[str] = frozenset({"de", "del", "la", "las", "los", "y", "da", "do", "dos"})
# Longitud mínima de un token para que un prefijo cuente como coincidencia. Con menos
# ("M" contra "Mirko") cualquier inicial matchearía cualquier nombre.
_MIN_PREFIX = 3
_MASK_CHARS = re.compile(r"[*·•#]+")
_NON_WORD = re.compile(r"[^a-z0-9\s]+")


def normalize_name(raw: str | None) -> list[str]:
    """Tokens comparables de un nombre: minúsculas, sin tildes, sin ruido."""
    if not raw:
        return []
    # NFKD + descarte de marcas: "Durán" → "duran", sin depender del locale.
    folded = unicodedata.normalize("NFKD", raw).encode("ascii", "ignore").decode("ascii").lower()
    cleaned = _NON_WORD.sub(" ", _MASK_CHARS.sub(" ", folded))
    return [token for token in cleaned.split() if token and token not in _NOISE]


def matches_beneficiary(expected: str | None, found: str | None) -> bool:
    """True si `found` (lo que dice el comprobante) puede ser `expected` (lo configurado).

    Sin nombre esperado configurado devuelve `False`: no se puede afirmar que el pago
    fue a la cuenta correcta, así que va a revisión humana. Lo mismo si el comprobante
    no trae destinatario legible.
    """
    expected_tokens = normalize_name(expected)
    found_tokens = normalize_name(found)
    if not expected_tokens or not found_tokens:
        return False
    if is_masked(found):
        return _matches_masked(expected_tokens, found_tokens)
    return all(_token_present(token, found_tokens) for token in expected_tokens)


def is_masked(raw: str | None) -> bool:
    """El layout ocultó parte del nombre ("M*** C***")."""
    return bool(raw) and bool(_MASK_CHARS.search(raw or ""))


def _matches_masked(expected_tokens: list[str], found_tokens: list[str]) -> bool:
    """Compara un nombre enmascarado, que solo deja ver iniciales.

    Se exige **la misma cantidad de palabras y el mismo conjunto de iniciales**: es lo
    máximo que se puede afirmar cuando el banco tapó el resto. Comparar el conjunto y
    no la secuencia tolera el orden invertido (unos layouts ponen el apellido primero).

    Es una señal débil por naturaleza — dos iniciales coinciden con muchos nombres — y
    por eso nunca decide sola: el comprobante también tiene que traer el monto exacto,
    una fecha reciente y un número de transacción que no se haya usado antes. Y el pago
    queda igual pendiente de confirmación humana.
    """
    if len(expected_tokens) != len(found_tokens):
        return False
    return sorted(token[0] for token in expected_tokens) == sorted(
        token[0] for token in found_tokens
    )


def _token_present(token: str, candidates: list[str]) -> bool:
    """El token aparece entre los candidatos, admitiendo truncamiento en cualquiera
    de los dos lados (el banco corta el campo, o la config guarda el nombre corto)."""
    for candidate in candidates:
        if token == candidate:
            return True
        long_enough = len(token) >= _MIN_PREFIX and len(candidate) >= _MIN_PREFIX
        if long_enough and (candidate.startswith(token) or token.startswith(candidate)):
            return True
    return False
