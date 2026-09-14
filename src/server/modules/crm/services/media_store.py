"""Almacenamiento de adjuntos salientes del takeover (#251).

Mismo set de formatos y validación que los materiales del catálogo (MIME + magic
bytes + ≤5 MB), pero sin fila `asset`: el archivo se persiste bajo
`media_root/crm/{org}/` (servido por el mount `/media`) y la URL pública viaja en
el mensaje espejado. WhatsApp descarga la media desde ese link.
"""

from __future__ import annotations

import os
import uuid
from dataclasses import dataclass

from server.config import get_settings
from server.modules.agent.services.asset_service import (
    ALLOWED_UPLOADS,
    MATERIAL_MAX_BYTES,
    sanitize_filename,
)
from server.shared.exceptions import ValidationException


@dataclass(frozen=True, slots=True)
class StoredMedia:
    media_type: str  # 'image' | 'document'
    url: str  # absolute public URL (WhatsApp fetches it)
    filename: str  # sanitized visible name (document sends)


def store_outbound_media(
    *, organization_id: uuid.UUID, content_type: str | None, filename: str | None, data: bytes
) -> StoredMedia:
    """Valida y persiste el adjunto; devuelve tipo + URL pública + nombre visible."""
    spec = ALLOWED_UPLOADS.get(content_type or "")
    if spec is None or not data.startswith(spec[2]):
        raise ValidationException("El archivo debe ser PDF, JPG o PNG")
    kind, ext, _ = spec
    if len(data) > MATERIAL_MAX_BYTES:
        raise ValidationException("El archivo supera el límite de 5 MB")

    settings = get_settings()
    directory = os.path.join(settings.media_root, "crm", str(organization_id))
    os.makedirs(directory, exist_ok=True)
    token = uuid.uuid4().hex
    with open(os.path.join(directory, f"{token}{ext}"), "wb") as handle:
        handle.write(data)

    return StoredMedia(
        media_type="image" if kind == "image" else "document",
        url=f"{settings.media_base_url}/media/crm/{organization_id}/{token}{ext}",
        filename=sanitize_filename(filename or f"documento{ext}", ext),
    )
