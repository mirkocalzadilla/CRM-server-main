"""Subida de materiales (PDF/JPG/PNG) → `asset`, hosteado en `media_root` (mismo
patrón que el QR del CRM): se guarda el archivo y se expone por
`{media_base_url}/media/...`.

Valida MIME + magic bytes + tamaño (≤5 MB por material, #108) y sanitiza el nombre
visible (los originales traen acentos/espacios/elipsis que romperían rutas/envío).
"""

from __future__ import annotations

import os
import re
import unicodedata
import uuid

import anyio
from sqlalchemy.ext.asyncio import AsyncSession

from server.config import get_settings
from server.modules.agent.domain.catalog_models import Asset
from server.modules.agent.repositories.asset_repository import AssetRepository
from server.shared.exceptions import NotFoundException, ValidationException

MATERIAL_MAX_BYTES = 5 * 1024 * 1024  # 5 MB por material (#108)

# content_type → (kind, extensión, magic bytes). Cubre los formatos del dropzone.
# Público: el CRM reusa el mismo set para los adjuntos del takeover (#251).
ALLOWED_UPLOADS = {
    "application/pdf": ("pdf", ".pdf", b"%PDF"),
    "image/jpeg": ("image", ".jpg", b"\xff\xd8\xff"),
    "image/png": ("image", ".png", b"\x89PNG\r\n\x1a\n"),
}


def sanitize_filename(name: str, ext: str = ".pdf") -> str:
    """Nombre visible ASCII-only, sin espacios/acentos/elipsis; conserva la extensión."""
    base = name.rsplit("/", 1)[-1].rsplit("\\", 1)[-1]
    stem = base[: -len(ext)] if base.lower().endswith(ext) else base
    normalized = unicodedata.normalize("NFKD", stem).encode("ascii", "ignore").decode("ascii")
    cleaned = re.sub(r"[^A-Za-z0-9._-]+", "-", normalized).strip("-._")
    return f"{cleaned or 'documento'}{ext}"


class AssetService:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session
        self._repo = AssetRepository(session)

    async def upload_material(
        self,
        *,
        organization_id: uuid.UUID,
        content_type: str | None,
        filename: str | None,
        data: bytes,
    ) -> Asset:
        spec = ALLOWED_UPLOADS.get(content_type or "")
        if spec is None or not data.startswith(spec[2]):
            raise ValidationException("El archivo debe ser PDF, JPG o PNG")
        kind, ext, _ = spec
        if len(data) > MATERIAL_MAX_BYTES:
            raise ValidationException("El archivo supera el límite de 5 MB")
        safe = sanitize_filename(filename or f"documento{ext}", ext)
        storage_ref = self._save(str(organization_id), ext, data)
        public_url = f"{get_settings().media_base_url}/media/{storage_ref}"
        asset = await self._repo.add(
            Asset(
                organization_id=organization_id,
                kind=kind,
                filename=safe,
                storage_ref=storage_ref,
                public_url=public_url,
                bytes=len(data),
            )
        )
        await self._session.commit()
        return asset

    def _save(self, org_id: str, ext: str, data: bytes) -> str:
        """Persiste el archivo bajo media_root/catalogo/{org}/{token}{ext}; devuelve el ref."""
        settings = get_settings()
        directory = os.path.join(settings.media_root, "catalogo", org_id)
        os.makedirs(directory, exist_ok=True)
        token = uuid.uuid4().hex
        with open(os.path.join(directory, f"{token}{ext}"), "wb") as handle:
            handle.write(data)
        return f"catalogo/{org_id}/{token}{ext}"

    async def delete_material(self, asset_id: uuid.UUID, organization_id: uuid.UUID) -> None:
        """Borra el material físicamente y de la base de datos, y re-sincroniza el
        snapshot del tenant (por si el material estaba enlazado a una categoría)."""
        from server.modules.agent.services.catalog_service import CatalogService

        asset = await self._repo.get(asset_id, organization_id)
        if asset is None:
            raise NotFoundException("Material no encontrado")

        storage_ref = asset.storage_ref

        await self._repo.delete(asset)
        await self._session.commit()
        await CatalogService(self._session).sync_tenant_snapshot(organization_id)

        # Delete physical file
        settings = get_settings()
        filepath = anyio.Path(settings.media_root) / storage_ref
        await filepath.unlink(missing_ok=True)
