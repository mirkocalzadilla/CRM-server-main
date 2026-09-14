"""Regenera como PNG 8-bit RGB las entradas emitidas antes del fix de #297.

Los PNG que escribía segno son grayscale de 1 bit y Meta los descarta al
descargarlos (exige "8-bit, RGB or RGBA"), así que reenviar una entrada ya
emitida volvería a mandar un mensaje que nunca llega. El nombre del archivo es
el token y el token es el contenido del QR: regenerarlo es determinístico y no
invalida nada — el QR resultante abre la misma entrada.

Idempotente: un PNG que ya es 8-bit RGB/RGBA no se toca.

Uso:

    docker compose exec backend python scripts/regenerate_qr_pngs.py --dry-run
    docker compose exec backend python scripts/regenerate_qr_pngs.py
"""

from __future__ import annotations

import os
import sys

from server.config import get_settings
from server.modules.crm.services.qr_image import render_qr_png
from server.shared.logger import configure_logging, get_logger

logger = get_logger(__name__)

_PNG_SIGNATURE = b"\x89PNG\r\n\x1a\n"
_RGB_COLOR_TYPES = {2, 6}  # truecolor / truecolor + alpha


def _is_8bit_rgb(path: str) -> bool:
    with open(path, "rb") as handle:
        header = handle.read(26)
    if len(header) < 26 or header[:8] != _PNG_SIGNATURE:
        return False
    return header[24] == 8 and header[25] in _RGB_COLOR_TYPES


def regenerate(*, dry_run: bool) -> tuple[int, int]:
    """Devuelve (regenerados, ya correctos)."""
    qr_root = os.path.join(get_settings().media_root, "qr")
    if not os.path.isdir(qr_root):
        return 0, 0
    regenerated = 0
    untouched = 0
    for org_id in sorted(os.listdir(qr_root)):
        org_dir = os.path.join(qr_root, org_id)
        if not os.path.isdir(org_dir):
            continue
        for filename in sorted(os.listdir(org_dir)):
            if not filename.endswith(".png"):
                continue
            path = os.path.join(org_dir, filename)
            if _is_8bit_rgb(path):
                untouched += 1
                continue
            logger.info("qr.regenerate", org_id=org_id, file=filename, dry_run=dry_run)
            if not dry_run:
                render_qr_png(filename.removesuffix(".png"), path)
            regenerated += 1
    return regenerated, untouched


def main() -> None:
    configure_logging()
    dry_run = "--dry-run" in sys.argv
    regenerated, untouched = regenerate(dry_run=dry_run)
    logger.info("qr.regenerate_done", dry_run=dry_run, regenerated=regenerated, untouched=untouched)


if __name__ == "__main__":
    main()
