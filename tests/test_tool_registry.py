"""Tests del ToolRegistry y el contrato ToolDefinition (puro, sin DB ni LLM)."""

from __future__ import annotations

import pytest

from server.modules.agent.domain.funnel_fsm import FunnelStage
from server.modules.agent.domain.tool_catalogue import DEFAULT_TOOLS, build_default_registry
from server.modules.agent.domain.tools import ToolContext, ToolNotFoundError


def test_registry_names_match_catalogue() -> None:
    registry = build_default_registry()
    assert set(registry.names()) == {t.name for t in DEFAULT_TOOLS}


def test_specs_expose_name_description_schema() -> None:
    specs = build_default_registry().specs()
    assert len(specs) == len(DEFAULT_TOOLS)
    for spec in specs:
        assert spec.name
        assert spec.description
        assert spec.input_schema["type"] == "object"


def test_get_unknown_raises() -> None:
    with pytest.raises(ToolNotFoundError):
        build_default_registry().get("does_not_exist")


def test_contains() -> None:
    registry = build_default_registry()
    assert "get_service" in registry
    assert "nope" not in registry


async def test_execute_dispatches_to_handler() -> None:
    registry = build_default_registry()
    ctx = ToolContext(
        funnel_stage=FunnelStage.NEW,
        config={"services": {"cursos": {"precio": "480 Bs"}}},
    )
    result = await registry.execute("get_service", ctx, {"linea": "cursos"})
    assert result["encontrado"] is True


async def test_execute_unknown_raises() -> None:
    registry = build_default_registry()
    ctx = ToolContext(funnel_stage=FunnelStage.NEW, config={})
    with pytest.raises(ToolNotFoundError):
        await registry.execute("nope", ctx, {})
