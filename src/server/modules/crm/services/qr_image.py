"""Generación de la imagen del QR de una entrada.

Aparte del servicio de entradas porque es I/O de archivos, no lógica de negocio: el
token es lo que identifica la entrada, y esto solo lo dibuja. Tenerlo separado también
hace que los tests puedan evitar escribir PNGs sin stubbear el servicio entero.
"""

from __future__ import annotations

import io
import os

import segno
from PIL import Image

from server.config import get_settings


def render_qr_png(token: str, path: str) -> None:
    """Renderiza el QR del token como PNG 8-bit RGB.

    Meta exige "8-bit, RGB or RGBA" para las imágenes de WhatsApp; el PNG nativo
    de segno es grayscale de 1 bit y el Cloud API acepta el envío (2xx) pero
    descarta el mensaje al descargar el archivo, sin que nada falle acá (#297).
    """
    buffer = io.BytesIO()
    segno.make(token).save(buffer, kind="png", scale=10)
    buffer.seek(0)
    with Image.open(buffer) as qr:
        qr.convert("RGB").save(path, format="PNG")


def save_qr(org_id: str, token: str) -> str:
    """Escribe el PNG del QR en `media_root` y devuelve su path relativo."""
    settings = get_settings()
    qr_dir = os.path.join(settings.media_root, "qr", org_id)
    os.makedirs(qr_dir, exist_ok=True)
    filename = f"{token}.png"
    render_qr_png(token, os.path.join(qr_dir, filename))
    return f"qr/{org_id}/{filename}"


def public_url(qr_ref: str) -> str:
    """URL pública de la imagen (la que recibe el lead y espeja el hilo)."""
    return f"{get_settings().media_base_url}/media/{qr_ref}"
