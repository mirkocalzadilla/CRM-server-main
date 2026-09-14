"""Almacenamiento del QR de pago de una organización.

Mismo patrón que los adjuntos del takeover (`media_store.py`): el archivo se persiste
bajo `media_root/payments/{org}/` y se sirve por el mount `/media`. Tiene que ser una
URL pública porque WhatsApp descarga la imagen desde ahí cada vez que el agente la
manda; no la sube el sistema, la va a buscar.

Dos decisiones que no son detalles:

- **Solo imágenes** (JPG/PNG). El lead recibe una foto para escanear, no un adjunto;
  aceptar PDF acá sería aceptar algo que el flujo de pago no puede mostrar.
- **Nombre con token nuevo en cada subida.** Reemplazar el QR cambia la URL, así que
  WhatsApp no puede seguir sirviendo la imagen anterior desde su cache. Con un nombre
  fijo, el lead podría recibir el QR viejo después del cambio.
"""

from __future__ import annotations

import contextlib
import os
import uuid
from dataclasses import dataclass

from server.config import get_settings
from server.modules.agent.services.asset_service import ALLOWED_UPLOADS, MATERIAL_MAX_BYTES
from server.shared.exceptions import ValidationException

# Subconjunto de imágenes del set común: el QR se muestra, no se descarga.
IMAGE_UPLOADS = {mime: spec for mime, spec in ALLOWED_UPLOADS.items() if spec[0] == "image"}


@dataclass(frozen=True, slots=True)
class StoredQr:
    storage_ref: str  # path relativo bajo media_root (lo que guarda la fila)
    url: str  # URL pública absoluta (la que recibe el lead)


def store_payment_qr(
    *, organization_id: uuid.UUID, content_type: str | None, data: bytes
) -> StoredQr:
    """Valida y persiste la imagen; devuelve el ref para la fila y la URL pública."""
    spec = IMAGE_UPLOADS.get(content_type or "")
    if spec is None or not data.startswith(spec[2]):
        raise ValidationException("El QR debe ser una imagen JPG o PNG")
    if len(data) > MATERIAL_MAX_BYTES:
        raise ValidationException("La imagen supera el límite de 5 MB")
    _, ext, _ = spec

    settings = get_settings()
    directory = os.path.join(settings.media_root, "payments", str(organization_id))
    os.makedirs(directory, exist_ok=True)
    storage_ref = f"payments/{organization_id}/{uuid.uuid4().hex}{ext}"
    with open(os.path.join(settings.media_root, storage_ref), "wb") as handle:
        handle.write(data)

    return StoredQr(storage_ref=storage_ref, url=f"{settings.media_base_url}/media/{storage_ref}")


def delete_payment_qr(storage_ref: str) -> None:
    """Borra el archivo anterior. Que ya no exista no es un error: el estado que manda
    es la fila, y un archivo faltante ya está donde se lo quiere dejar."""
    with contextlib.suppress(OSError):
        os.remove(os.path.join(get_settings().media_root, storage_ref))
