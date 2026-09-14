"""Tests del normalizador de markdown saliente para WhatsApp (red de seguridad B2)."""

from __future__ import annotations

from server.modules.agent.domain.whatsapp_format import to_whatsapp_text


def test_double_asterisk_becomes_whatsapp_bold() -> None:
    assert to_whatsapp_text("dale **ya** mismo") == "dale *ya* mismo"


def test_underscore_bold_becomes_whatsapp_italic() -> None:
    assert to_whatsapp_text("son __480 Bs__") == "son _480 Bs_"


def test_heading_marker_is_stripped() -> None:
    assert to_whatsapp_text("# Curso\nempieza el 12") == "Curso\nempieza el 12"


def test_plain_text_is_untouched() -> None:
    body = "Son 480 Bs 🙂 te queda La Paz?"
    assert to_whatsapp_text(body) == body


def test_single_asterisk_is_preserved() -> None:
    # `*x*` ya es negrita nativa de WhatsApp: no se toca.
    assert to_whatsapp_text("usá *esto*") == "usá *esto*"


def test_bold_spanning_words() -> None:
    assert to_whatsapp_text("**La Paz, 12 de julio**") == "*La Paz, 12 de julio*"


def test_strips_opening_marks() -> None:
    # A8: la persona prohíbe ¡/¿; el modelo igual los filtra → se quitan a la salida.
    assert to_whatsapp_text("¿Te gustaría saber algo más?") == "Te gustaría saber algo más?"
    assert to_whatsapp_text("¡Hola! ¿Qué ciudad te queda?") == "Hola! Qué ciudad te queda?"


def test_keeps_closing_marks() -> None:
    # Solo los signos de apertura se eliminan; ?/! de cierre quedan.
    assert to_whatsapp_text("Listo! Te ayudo?") == "Listo! Te ayudo?"
