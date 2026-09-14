"""Tests del contexto de runtime inyectado al system prompt (fecha real, hora Bolivia)."""

from __future__ import annotations

import random
from datetime import datetime
from zoneinfo import ZoneInfo

import pytest

from server.modules.agent.domain.runtime_context import (
    current_date_line,
    opening_greeting,
    salutation,
    with_runtime_context,
)

_LA_PAZ = ZoneInfo("America/La_Paz")


def _at(hour: int, minute: int = 0) -> datetime:
    return datetime(2026, 7, 28, hour, minute, tzinfo=_LA_PAZ)


def test_current_date_line_formats_injected_now() -> None:
    line = current_date_line(datetime(2026, 7, 12, 9, 0, tzinfo=_LA_PAZ))
    assert "12 de julio de 2026" in line
    assert "2026-07-12" in line  # ISO for unambiguous time math


def test_with_runtime_context_prepends_date_and_keeps_prompt() -> None:
    out = with_runtime_context("PERSONA", datetime(2026, 1, 5, tzinfo=_LA_PAZ))
    assert out.startswith("Contexto: hoy es")
    assert out.endswith("PERSONA")
    assert "5 de enero de 2026" in out


def test_with_runtime_context_includes_safety_guardrail() -> None:
    # A7: el guardrail anti-inyección viaja siempre, entre la fecha y la persona.
    out = with_runtime_context("PERSONA", datetime(2026, 1, 5, tzinfo=_LA_PAZ))
    assert "Reglas de seguridad" in out
    assert "no te consta" in out and "derivá al equipo" in out
    # va antes de la persona editable (que no puede borrarlo desde /crm)
    assert out.index("Reglas de seguridad") < out.index("PERSONA")


def test_lead_name_injected_when_known() -> None:
    # #91: con el nombre ya capturado, el contexto lo inyecta para personalizar la
    # despedida/confirmación, entre la fecha y el guardrail.
    out = with_runtime_context(
        "PERSONA", datetime(2026, 1, 5, tzinfo=_LA_PAZ), lead_name="Juan Pérez"
    )
    assert "el lead se llama Juan Pérez" in out
    assert "personalizar la despedida" in out
    assert out.index("Juan Pérez") < out.index("Reglas de seguridad")


def test_lead_name_absent_when_unknown() -> None:
    # Sin nombre (lead nuevo) no se inyecta la línea de nombre.
    out = with_runtime_context("PERSONA", datetime(2026, 1, 5, tzinfo=_LA_PAZ))
    assert "el lead se llama" not in out


@pytest.mark.parametrize(
    ("hour", "minute", "expected"),
    [
        (5, 0, "Buenos días"),
        (11, 59, "Buenos días"),
        (12, 0, "Buenas tardes"),
        (18, 59, "Buenas tardes"),
        (19, 0, "Buenas noches"),
        (23, 30, "Buenas noches"),
        (3, 0, "Buenas noches"),  # madrugada cuenta como noche, no como día
        (4, 59, "Buenas noches"),
    ],
)
def test_salutation_time_slots(hour: int, minute: int, expected: str) -> None:
    # #265: cortes de franja acordados (dias 05-11:59, tardes 12-18:59, noches 19-04:59).
    assert salutation(_at(hour, minute)) == expected


def test_opening_greeting_composes_and_rotates_courtesy() -> None:
    # #265: saludo resuelto = franja + cortesía del pool + 😊; la cortesía rota.
    greeting = opening_greeting(_at(15), rng=random.Random(1))
    assert greeting.startswith("Buenas tardes, ")
    assert greeting.endswith("😊")
    seen = {opening_greeting(_at(15), rng=random.Random(seed)) for seed in range(20)}
    assert len(seen) > 1  # no siempre la misma cortesía


def test_first_turn_greeting_injected_only_when_passed() -> None:
    # #265: la línea con el saludo exacto viaja solo en el primer turno (gate del caller).
    greeting = "Buenas tardes, qué tal? 😊"
    out = with_runtime_context("PERSONA", _at(15), first_turn_greeting=greeting)
    assert "es el primer mensaje del lead" in out
    assert greeting in out
    assert out.index(greeting) < out.index("Reglas de seguridad")
    without = with_runtime_context("PERSONA", _at(15))
    assert "primer mensaje del lead" not in without


def test_default_now_uses_current_year() -> None:
    # No injected `now` → real clock (Bolivia). Smoke check it produces a date line.
    line = current_date_line()
    assert "hoy es" in line
    assert str(datetime.now(_LA_PAZ).year) in line
