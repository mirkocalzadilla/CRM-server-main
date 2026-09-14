"""Console smoke para M4 (correr a mano). Ejercita el AgentService de punta
a punta con STUBS: store en memoria, sender que imprime, LLMPort con Turns canned.
Verifica el flujo detectado + la transición de funnel, sin WhatsApp/DB/LLM real.

Uso:  python scripts/m4_smoke.py
"""

from __future__ import annotations

import asyncio
import sys
import uuid
from dataclasses import dataclass, field

from server.modules.agent.domain.funnel_fsm import FunnelStage
from server.modules.agent.domain.llm_port import Message, Role, ToolSpec, ToolUse, Turn
from server.modules.agent.domain.ports import ConversationSnapshot
from server.modules.agent.domain.tool_catalogue import build_default_registry
from server.modules.agent.services.agent_service import AgentService
from server.modules.agent.services.router import Flow

_TENANT = uuid.uuid4()
_CONFIG: dict[str, object] = {
    "services": {"cursos": {"precio": "480 Bs", "ciudades": ["La Paz"], "fechas": ["2026-07-12"]}},
    "faq": {"ubicacion": "Av. Siempre Viva 123"},
}
_SYSTEM = "Sos el asistente de Mirko. Voseo, corto, sin signos de apertura."


@dataclass
class _Conv:
    stage: FunnelStage = FunnelStage.NEW
    is_ai_active: bool = True
    window: list[Message] = field(default_factory=list)


class StubStore:
    """In-memory ConversationStore. Persists the turn so the next one sees it."""

    def __init__(self) -> None:
        self._convs: dict[uuid.UUID, _Conv] = {}
        self.agent_id = uuid.uuid4()

    def ensure(self, conversation_id: uuid.UUID) -> _Conv:
        return self._convs.setdefault(conversation_id, _Conv())

    def push_inbound(self, conversation_id: uuid.UUID, text: str) -> None:
        self.ensure(conversation_id).window.append(Message(role=Role.USER, text=text))

    async def load(
        self, conversation_id: uuid.UUID, tenant_id: uuid.UUID
    ) -> ConversationSnapshot | None:
        conv = self._convs.get(conversation_id)
        if conv is None:
            return None
        return ConversationSnapshot(
            external_id="+59170000000",
            agent_id=self.agent_id,
            funnel_stage=conv.stage,
            is_ai_active=conv.is_ai_active,
            system_prompt=_SYSTEM,
            config=_CONFIG,
            messages_window=tuple(conv.window[-10:]),
        )

    async def save_turn(
        self,
        conversation_id: uuid.UUID,
        tenant_id: uuid.UUID,
        *,
        reply_text: str,
        funnel_stage: FunnelStage,
        is_ai_active: bool,
        handoff_reason: str | None = None,
    ) -> None:
        conv = self.ensure(conversation_id)
        conv.stage = funnel_stage
        conv.is_ai_active = is_ai_active
        if reply_text:
            conv.window.append(Message(role=Role.ASSISTANT, text=reply_text))


class StubSender:
    async def send_text(self, to: str, body: str) -> None:
        print(f"    [WA→{to}] {body}")

    async def send_image(self, to: str, link: str, caption: str = "") -> None:
        print(f"    [WA→{to}] [img] {caption} ({link})")

    async def send_document(self, to: str, link: str, filename: str, caption: str = "") -> None:
        print(f"    [WA→{to}] [doc {filename}] {caption} ({link})")


@dataclass
class StubLLM:
    """Canned LLMPort. Router-classify calls (tool 'classify_intent' present) return
    `classify_flow`; loop calls pop the next canned Turn."""

    loop_turns: list[Turn] = field(default_factory=list)
    classify_flow: Flow = Flow.UNKNOWN

    async def complete(
        self,
        *,
        system: str,
        messages: list[Message],
        tools: list[ToolSpec],
        temperature: float | None = None,
    ) -> Turn:
        if any(t.name == "classify_intent" for t in tools):
            return Turn(
                text="",
                tool_uses=(
                    ToolUse(
                        id="cls",
                        name="classify_intent",
                        input={"flow": self.classify_flow.value, "confidence": 0.9},
                    ),
                ),
                stop_reason="tool_use",
            )
        if self.loop_turns:
            return self.loop_turns.pop(0)
        return Turn(text="(sin respuesta)", tool_uses=(), stop_reason="end_turn")


def _use(name: str, payload: dict[str, object]) -> ToolUse:
    return ToolUse(id=f"tu_{name}", name=name, input=payload)


def _tt(text: str, *uses: ToolUse) -> Turn:
    return Turn(text, uses, "tool_use")


def _txt(text: str) -> Turn:
    return Turn(text, (), "end_turn")


async def _drive(
    store: StubStore, conv_id: uuid.UUID, text: str, *turns: Turn, classify: Flow = Flow.UNKNOWN
) -> None:
    store.push_inbound(conv_id, text)
    orch = AgentService(
        store=store,
        llm=StubLLM(list(turns), classify),
        registry=build_default_registry(),
        sender=StubSender(),
    )
    print(f"  lead: {text!r}")
    await orch.process_message(conv_id, _TENANT)
    conv = store.ensure(conv_id)
    print(f"    → funnel={conv.stage.value} is_ai_active={conv.is_ai_active}")


async def _scenario_happy_path(store: StubStore) -> None:
    print("== Camino feliz: saludo → calificación → derivación por pago ==")
    c = uuid.uuid4()
    await _drive(store, c, "hola")  # GREETING (0 IA)
    await _drive(
        store,
        c,
        "cuanto cuesta y en que ciudades?",
        _tt(
            "",
            _use("get_service", {"linea": "cursos"}),
            _use("set_lead_stage", {"etapa": "qualifying", "razon": "ciudad"}),
        ),
        _txt("Son 480 Bs 🙂 hacemos en La Paz el 12/07. Te queda?"),
    )
    await _drive(
        store,
        c,
        "quiero inscribirme",
        _tt("", _use("set_lead_stage", {"etapa": "qualified", "razon": "inscribe"})),
        _txt("Buenisimo! Aca esta el QR. Cuando pagues, mandame el comprobante 🙌"),
    )
    # Ack rides with the handoff tool turn: the loop breaks on handoff.
    await _drive(
        store,
        c,
        "ya pague, aca esta el comprobante",
        _tt(
            "Listo! El equipo valida tu pago y te confirma 🙌",
            _use("handoff_to_human", {"motivo": "payment_validation"}),
        ),
    )
    await _drive(store, c, "hola? siguen ahi?")  # silenciado: sin respuesta


async def _scenario_explicit_handoff(store: StubStore) -> None:
    print("\n== Derivación explícita (determinística, 0 IA) ==")
    await _drive(store, uuid.uuid4(), "quiero hablar con alguien del equipo")


async def _scenario_llm_fallback(store: StubStore) -> None:
    print("\n== Fallback Haiku del router (mensaje ambiguo) ==")
    c = uuid.uuid4()
    store.ensure(c).stage = FunnelStage.ENGAGING  # ya saludó antes
    await _drive(
        store,
        c,
        "mmm y eso",
        _tt("", _use("get_service", {"linea": "cursos"})),
        _txt("Son 480 Bs 🙂"),
        classify=Flow.SERVICE,
    )


async def main() -> None:
    # Windows consoles default to cp1252; force UTF-8 so emojis/arrows print.
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    store = StubStore()
    await _scenario_happy_path(store)
    await _scenario_explicit_handoff(store)
    await _scenario_llm_fallback(store)


if __name__ == "__main__":
    asyncio.run(main())
