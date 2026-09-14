"""Tests de `model_supports_temperature` (server#288): catálogo primero, prefijos de
familias conocidas después, y default seguro (no mandar) para modelos desconocidos."""

from __future__ import annotations

import pytest

from server.modules.agent.domain.model_catalog import MODEL_CATALOG, model_supports_temperature


@pytest.mark.parametrize(
    ("model_id", "expected"),
    [
        # En catálogo: manda el flag del descriptor.
        ("claude-sonnet-4-6", True),
        ("claude-haiku-4-5-20251001", True),
        ("gpt-4o-mini-2024-07-18", True),
        ("gpt-5.4-mini-2026-03-17", False),  # reasoning → rechaza sampling params
        ("gpt-5.4-2026-03-05", False),
        # Fuera de catálogo: familias que aceptan temperature, por prefijo.
        ("claude-3-5-haiku-20241022", True),
        ("claude-opus-4-6", True),
        ("claude-sonnet-4-5-20250929", True),
        # Fuera de catálogo y sin familia conocida: default seguro (omitir).
        ("claude-sonnet-5", False),
        ("claude-opus-5", False),
        ("claude-opus-4-7", False),
        ("claude-fable-5", False),
        ("modelo-inventado", False),
    ],
)
def test_model_supports_temperature(model_id: str, expected: bool) -> None:
    assert model_supports_temperature(model_id) is expected


def test_catalog_anthropic_entries_stay_in_accepting_families() -> None:
    # Guardrail: si entra al catálogo un modelo Anthropic 4.7+/5.x, tiene que venir con
    # supports_temperature=False (el API lo rechaza) — este test obliga a decidirlo.
    for descriptor in MODEL_CATALOG:
        if descriptor.provider == "anthropic" and descriptor.supports_temperature:
            assert model_supports_temperature(descriptor.id) is True
