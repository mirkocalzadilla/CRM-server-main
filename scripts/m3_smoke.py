"""Console smoke para M3 (correr a mano). Ejercita el ToolRegistry y el contrato
ToolDefinition con inputs de ejemplo, y luego hace UNA llamada real (barata) al
provider configurado (`LLM_PROVIDER`) si su API key está seteada. Sin DB, sin
WhatsApp.

Uso:  python scripts/m3_smoke.py
"""

from __future__ import annotations

import asyncio

from server.config import get_settings
from server.modules.agent.domain.funnel_fsm import FunnelStage
from server.modules.agent.domain.llm_port import Message, Role
from server.modules.agent.domain.tool_catalogue import build_default_registry
from server.modules.agent.domain.tools import ToolContext
from server.modules.agent.services.llm_factory import build_llm

_EXAMPLE_CONFIG: dict[str, object] = {
    "services": {
        "cursos": {
            "precio": "480 Bs",
            "ciudades": ["La Paz", "Santa Cruz"],
            "fechas": ["2026-07-12"],
        },
    },
    "faq": {"ubicacion": "Av. Siempre Viva 123", "que llevar": "tu laptop"},
}

_EXAMPLE_INPUTS: list[tuple[str, dict[str, object]]] = [
    ("get_service", {"linea": "cursos"}),
    ("consultar_faq", {"pregunta": "donde es la ubicacion?"}),
    ("set_lead_stage", {"etapa": "engaging", "razon": "saludo"}),
    ("set_lead_stage", {"etapa": "qualified", "razon": "salto invalido new->qualified"}),
    ("handoff_to_human", {"motivo": "explicit_request"}),
    ("out_of_scope", {"motivo": "fuera de Bolivia"}),
]


async def _exercise_tools() -> None:
    registry = build_default_registry()
    print("== Registry ==")
    print("tools:", registry.names())
    print("\n== Tool runs (ctx funnel_stage=new) ==")
    ctx = ToolContext(funnel_stage=FunnelStage.NEW, config=_EXAMPLE_CONFIG)
    for name, payload in _EXAMPLE_INPUTS:
        result = await registry.execute(name, ctx, payload)
        print(f"  {name}({payload}) -> {result}")


async def _one_llm_call() -> None:
    settings = get_settings()
    key = (
        settings.openai_api_key if settings.llm_provider == "openai" else settings.anthropic_api_key
    )
    if not key:
        print(
            f"\n[skip] API key del provider '{settings.llm_provider}' no seteada — omito la llamada real."
        )
        return
    print(f"\n== 1 llamada real ({settings.llm_provider}) ==")
    adapter = build_llm(settings, role="loop")
    registry = build_default_registry()
    turn = await adapter.complete(
        system="Sos el asistente de Mirko. Respondes corto, en voseo, sin signos de apertura.",
        messages=[Message(role=Role.USER, text="hola, cuanto cuesta el curso?")],
        tools=registry.specs(),
    )
    print("stop_reason:", turn.stop_reason)
    print("text:", turn.text)
    print("tool_uses:", [(u.name, u.input) for u in turn.tool_uses])


async def main() -> None:
    await _exercise_tools()
    await _one_llm_call()


if __name__ == "__main__":
    asyncio.run(main())
