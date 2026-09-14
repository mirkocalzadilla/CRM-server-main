"""Deterministic turn router (§7) → `{flow, confidence}`.

Layer 0 (rules/keywords/commands) resolves the turn for **0 IA cost**. Only when
confidence falls below the threshold does layer 1 fall back to a single Haiku
structured-output call to classify the flow. The router looks at THE TURN; the
funnel looks at the lead; never mix them (§2). It proposes a flow — the
orchestrator dispatches and the FSM still validates any funnel change.
"""

from __future__ import annotations

import enum
import json
from dataclasses import dataclass

from server.modules.agent.domain.llm_port import LLMPort, Message, Role, ToolSpec

CONFIDENCE_THRESHOLD = 0.6


class Flow(enum.StrEnum):
    """Coarse turn intent. GREETING/HANDOFF are deterministic (templated, 0 IA);
    the rest run the generative tool-loop, where the model picks the tool (§5.2).
    """

    GREETING = "greeting"
    HANDOFF = "handoff"
    SERVICE = "service"
    FAQ = "faq"
    QUALIFY = "qualify"
    UNKNOWN = "unknown"


GENERATIVE_FLOWS = frozenset({Flow.SERVICE, Flow.FAQ, Flow.QUALIFY, Flow.UNKNOWN})


@dataclass(frozen=True, slots=True)
class RouteDecision:
    flow: Flow
    confidence: float
    source: str  # "rules" | "llm"


# Keyword tables (lowercased, accent-insensitive match). Order = priority.
_GREETING = ("hola", "buenas", "buenos dias", "buen dia", "que tal", "hello", "hi")
# Explicit human-handoff intent (A6/#78). Substring match, so "persona" also catches
# "una persona"/"hablar con una persona", "alguien" catches "pásame con alguien", and
# bare "atencion"/"atención" subsume "atención al cliente".
_HANDOFF = (
    "asesor",
    "humano",
    "persona",
    "alguien",
    "agente",
    "operador",
    "encargado",
    "representante",
    "supervisor",
    "reclamo",
    "queja",
    "atencion",
    "atención",
    "hablar con el equipo",
)
_QUALIFY = ("inscrib", "anotar", "como pago", "cómo pago", "pagar", "comprobante", "reservar")
_SERVICE = (
    "precio",
    "cuesta",
    "cuanto",
    "cuánto",
    "curso",
    "servicio",
    "trata",
    "ensena",
    "enseña",
    # Campaign leads open with the event, not the course ("vengo por el evento",
    # "busco entradas"): without these the first turn falls to the canned greeting (#294).
    "evento",
    "entrada",
)
_FAQ = ("ubicacion", "ubicación", "donde", "dónde", "cuando", "cuándo", "cupo", "que llevar")

_CLASSIFY_TOOL = ToolSpec(
    name="classify_intent",
    description=(
        "Clasifica la intención del turno del lead en uno de los flujos. Usá "
        "'greeting' para saludos, 'handoff' si pide un humano, 'service' para "
        "precio/servicio, 'faq' para logística, 'qualify' si quiere inscribirse, "
        "'unknown' si no encaja."
    ),
    input_schema={
        "type": "object",
        "properties": {
            "flow": {"type": "string", "enum": [f.value for f in Flow]},
            "confidence": {"type": "number"},
        },
        "required": ["flow", "confidence"],
    },
)

_CLASSIFY_SYSTEM = (
    "Sos un clasificador de intención. Llamá a classify_intent con el flujo del "
    "turno y tu confianza (0..1). No respondas texto."
)


class Router:
    """Rules first; Haiku structured-output only as a sub-threshold fallback."""

    def __init__(self, llm: LLMPort, *, threshold: float = CONFIDENCE_THRESHOLD) -> None:
        self._llm = llm
        self._threshold = threshold

    async def route(self, text: str, *, is_first_turn: bool) -> RouteDecision:
        decision = self._rules(text, is_first_turn=is_first_turn)
        if decision.confidence < self._threshold:
            decision = await self._classify(text)
        return _demote_stale_greeting(decision, is_first_turn=is_first_turn)

    def _rules(self, text: str, *, is_first_turn: bool) -> RouteDecision:
        norm = text.strip().lower()
        # Explicit human request wins over the first-contact greeting (it may fire
        # at any turn — FLUJO §1).
        if _matches(norm, _HANDOFF):
            return RouteDecision(Flow.HANDOFF, 0.95, "rules")
        # A content-bearing first turn carries a real intent (leads from the ad
        # campaign open with "info del curso"/"cuánto cuesta"): answer it on its
        # merits instead of swallowing it into the canned catalog menu. Intent
        # keywords therefore beat the first-contact greeting; GREETING stays the
        # fallback for a bare "hola" or an unmatched first message (#).
        if _matches(norm, _QUALIFY):
            return RouteDecision(Flow.QUALIFY, 0.8, "rules")
        if _matches(norm, _SERVICE):
            return RouteDecision(Flow.SERVICE, 0.8, "rules")
        if _matches(norm, _FAQ):
            return RouteDecision(Flow.FAQ, 0.75, "rules")
        if is_first_turn or _matches(norm, _GREETING):
            return RouteDecision(Flow.GREETING, 0.9, "rules")
        return RouteDecision(Flow.UNKNOWN, 0.3, "rules")

    async def _classify(self, text: str) -> RouteDecision:
        turn = await self._llm.complete(
            system=_CLASSIFY_SYSTEM,
            messages=[Message(role=Role.USER, text=text)],
            tools=[_CLASSIFY_TOOL],
        )
        for use in turn.tool_uses:
            if use.name == "classify_intent":
                return _decision_from_tool(use.input)
        return RouteDecision(Flow.UNKNOWN, 0.5, "llm")


def _demote_stale_greeting(decision: RouteDecision, *, is_first_turn: bool) -> RouteDecision:
    """A greeting only makes sense on first contact. Mid-conversation (e.g. a returning
    lead who reactivated the agent and says "hola") it must continue with context, not
    replay the canned template — demote to the generative flow (SPEC_UAT_remediation
    New#1c). Applies to both the rules and the LLM-classify path."""
    if decision.flow is Flow.GREETING and not is_first_turn:
        return RouteDecision(Flow.UNKNOWN, decision.confidence, decision.source)
    return decision


def _matches(text: str, keywords: tuple[str, ...]) -> bool:
    return any(word in text for word in keywords)


def _decision_from_tool(tool_input: dict[str, object]) -> RouteDecision:
    raw_flow = str(tool_input.get("flow", Flow.UNKNOWN.value))
    try:
        flow = Flow(raw_flow)
    except ValueError:
        flow = Flow.UNKNOWN
    try:
        confidence = float(tool_input.get("confidence", 0.5))  # type: ignore[arg-type]
    except (TypeError, ValueError):
        confidence = 0.5
    return RouteDecision(flow, confidence, "llm")


# Exposed for callers that want to serialize a decision (logging/turn metadata).
def decision_to_json(decision: RouteDecision) -> str:
    return json.dumps(
        {"flow": decision.flow.value, "confidence": decision.confidence, "source": decision.source}
    )
