"""Leer del disco el comprobante que el lead mandó, y saber qué es.

El webhook guarda el binario con la extensión que corresponde al mime… salvo cuando el
mime es desconocido, que cae a `.bin`. Así que la extensión **no** alcanza para decidir
si mandarlo como imagen o como PDF. Desde CR3 el mime real se persiste en el mensaje,
pero los mensajes anteriores no lo tienen: para esos se detecta por los magic bytes del
archivo, que es la fuente más confiable de todas (una foto renombrada a `.pdf` seguiría
siendo una foto).

Un archivo que no está, no se puede leer o no es un tipo soportado **no es un error del
sistema**: es un comprobante que no se pudo validar solo, y el caller lo trata como
check fallido → revisión humana. Nunca tumba el worker.
"""

from __future__ import annotations

import hashlib
import os
from dataclasses import dataclass

from server.config import get_settings
from server.modules.agent.domain.vision_port import is_supported_mime
from server.shared.logger import get_logger

logger = get_logger(__name__)

# Firmas de los tipos que puede mandar un lead. Se detectan por contenido porque es lo
# único que no miente: ni la extensión ni el mime que declara WhatsApp son confiables.
_MAGIC: tuple[tuple[bytes, str], ...] = (
    (b"\xff\xd8\xff", "image/jpeg"),
    (b"\x89PNG\r\n\x1a\n", "image/png"),
    (b"%PDF-", "application/pdf"),
)
_WEBP_PREFIX = b"RIFF"
_WEBP_TAG = b"WEBP"
# Tope de tamaño: un comprobante es una captura de pantalla. Más que esto es otra cosa
# (un video, un PDF gigante) y no vale gastar una llamada de visión ni memoria en leerlo.
MAX_RECEIPT_BYTES = 10 * 1024 * 1024


@dataclass(frozen=True, slots=True)
class ReceiptFile:
    content: bytes
    mime_type: str
    sha256: str


def detect_mime(content: bytes) -> str | None:
    """Mime real del binario según sus magic bytes, o `None` si no se reconoce."""
    for signature, mime in _MAGIC:
        if content.startswith(signature):
            return mime
    if content[:4] == _WEBP_PREFIX and content[8:12] == _WEBP_TAG:
        return "image/webp"
    return None


def load_receipt(media_path: str | None, declared_mime: str | None = None) -> ReceiptFile | None:
    """Lee el comprobante desde `media_root`. `None` si no se puede usar.

    El mime se resuelve por contenido y **solo se cae al declarado** si los magic bytes
    no dicen nada: un archivo cuyo contenido no coincide con lo que dice ser es
    exactamente el caso en que no hay que confiar en la declaración.
    """
    if not media_path:
        return None
    root = get_settings().media_root
    # `media_path` lo genera el webhook con "/" literal; se normaliza para el FS local.
    full_path = os.path.join(root, *media_path.split("/"))
    if not os.path.isfile(full_path):
        logger.warning("receipt.media_missing", media_path=media_path)
        return None
    size = os.path.getsize(full_path)
    if size > MAX_RECEIPT_BYTES:
        logger.warning("receipt.media_too_large", media_path=media_path, bytes=size)
        return None
    try:
        with open(full_path, "rb") as handle:
            content = handle.read()
    except OSError as exc:
        logger.warning("receipt.media_unreadable", media_path=media_path, error=str(exc))
        return None
    if not content:
        logger.warning("receipt.media_empty", media_path=media_path)
        return None

    mime = detect_mime(content) or declared_mime
    if mime is None or not is_supported_mime(mime):
        logger.warning("receipt.media_unsupported", media_path=media_path, mime=mime)
        return None
    return ReceiptFile(content=content, mime_type=mime, sha256=hashlib.sha256(content).hexdigest())
