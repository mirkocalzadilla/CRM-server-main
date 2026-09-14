"""El PNG de la entrada tiene que ser 8-bit RGB (#297).

Meta exige "8-bit, RGB or RGBA" para las imágenes de WhatsApp y descarta el
mensaje **asíncronamente** — después de aceptar el envío con 2xx — si el archivo
no cumple. El PNG nativo de segno es grayscale de 1 bit: exactamente ese caso, y
como el envío "sale bien" el stage avanzaba y el hilo espejaba una entrada que el
lead nunca recibía. Se verifica sobre los bytes del IHDR (offset 24 = bit depth,
25 = color type), que es lo que Meta valida, sin depender de Pillow en el assert.
"""

from __future__ import annotations

import struct
from pathlib import Path
from types import SimpleNamespace

import pytest
import segno

from server.modules.crm.services import qr_image

_PNG_SIGNATURE = b"\x89PNG\r\n\x1a\n"


def _ihdr(data: bytes) -> tuple[int, int, int]:
    """(width, bit depth, color type) del IHDR."""
    width, _height = struct.unpack(">II", data[16:24])
    return width, data[24], data[25]


def test_save_qr_writes_8bit_rgb_png(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(qr_image, "get_settings", lambda: SimpleNamespace(media_root=str(tmp_path)))
    ref = qr_image.save_qr("org-1", "token-abc")
    assert ref == "qr/org-1/token-abc.png"

    data = (tmp_path / "qr" / "org-1" / "token-abc.png").read_bytes()
    assert data[:8] == _PNG_SIGNATURE
    width, bit_depth, color_type = _ihdr(data)
    assert width > 0
    assert bit_depth == 8
    assert color_type in (2, 6)  # truecolor RGB / RGBA


def test_render_qr_png_converts_a_1bit_file_in_place(tmp_path: Path) -> None:
    # Una entrada emitida antes del fix: el PNG 1-bit de segno ya en disco. El
    # reenvío reusa el archivo, así que regenerarlo en el mismo path es el camino
    # de reparación (scripts/regenerate_qr_pngs.py).
    path = tmp_path / "token-old.png"
    segno.make("token-old").save(str(path), kind="png", scale=10)
    _width, bit_depth, _color_type = _ihdr(path.read_bytes())
    assert bit_depth == 1  # el bug que motiva el fix

    qr_image.render_qr_png("token-old", str(path))

    data = path.read_bytes()
    assert data[:8] == _PNG_SIGNATURE
    _width, bit_depth, color_type = _ihdr(data)
    assert bit_depth == 8
    assert color_type in (2, 6)
