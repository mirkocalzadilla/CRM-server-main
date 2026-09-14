"""Veredictos del escaneo en la puerta, y qué es lo que se escaneó.

Todo lo de acá es puro: clasificar un token y nombrar los rechazos. Lo importante del
diseño es que **cada rechazo dice por qué**. En la puerta hay una fila esperando y quien
atiende necesita saber qué hacer con la persona que tiene enfrente: "ya la usaron a las
19:40" y "esa entrada es del sábado" llevan a conversaciones completamente distintas, y
un "inválido" genérico no sirve para ninguna.
"""

from __future__ import annotations

import re
from enum import StrEnum

# Los tokens de entrada son uuid4 (decidido así para que el QR no lleve ningún dato del
# lead: lo que se muestra en la puerta se lee de la base, no del papel).
_UUID4 = re.compile(
    r"^[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$", re.IGNORECASE
)


class RedeemStatus(StrEnum):
    """Resultado del escaneo. El front pinta verde, ámbar o rojo según esto."""

    OK = "ok"
    # Válida pero sin evento: se emitió antes de que los eventos existieran. Es legítima,
    # así que pasa — con una advertencia para quien atiende.
    LEGACY = "legacy"
    ALREADY_USED = "already_used"
    WRONG_EVENT = "wrong_event"
    REVOKED = "revoked"
    NOT_FOUND = "not_found"
    # Lo escaneado es el QR **de pago** del banco, no una entrada. Es el error más
    # probable en la puerta: el lead abre la conversación y muestra la primera imagen.
    PAYMENT_QR = "payment_qr"


def looks_like_entry_token(raw: str) -> bool:
    """El texto tiene la forma de un token de entrada."""
    return bool(_UUID4.match(raw.strip()))


def looks_like_payment_qr(raw: str) -> bool:
    """El texto tiene la forma del QR de pago de un banco boliviano.

    Verificado sobre el QR real en uso: el payload es un blob cifrado, `base64` de 256
    bytes seguido de `|` y un hash hexadecimal. No es EMVCo legible, pero **para
    distinguirlo de un uuid4 alcanza y sobra**: es largo, y trae caracteres que un uuid
    no puede tener.

    Se es deliberadamente laxo — cualquier cosa larga con `|` o con caracteres de base64
    es, en la puerta, "no es una entrada, y muy probablemente sea el comprobante de
    pago". Ese mensaje resuelve la situación; un "token inválido" no.
    """
    text = raw.strip()
    if len(text) < 40:
        return False
    return "|" in text or bool(re.search(r"[+/=]", text))
