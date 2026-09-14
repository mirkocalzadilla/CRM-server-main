"""Tests del router determinístico + fallback Haiku (puro, LLM stubbeado)."""

from __future__ import annotations

from server.modules.agent.domain.llm_port import Message, ToolSpec, ToolUse, Turn
from server.modules.agent.services.router import Flow, Router


class _StubLLM:
    """Returns a canned classify_intent tool_use; records whether it was called."""

    def __init__(self, flow: Flow, confidence: float = 0.9) -> None:
        self._flow = flow
        self._confidence = confidence
        self.calls = 0

    async def complete(
        self,
        *,
        system: str,
        messages: list[Message],
        tools: list[ToolSpec],
        temperature: float | None = None,
    ) -> Turn:
        self.calls += 1
        return Turn(
            text="",
            tool_uses=(
                ToolUse(
                    id="c",
                    name="classify_intent",
                    input={"flow": self._flow.value, "confidence": self._confidence},
                ),
            ),
            stop_reason="tool_use",
        )


async def test_first_turn_is_greeting_without_llm() -> None:
    llm = _StubLLM(Flow.UNKNOWN)
    decision = await Router(llm).route("cualquier cosa", is_first_turn=True)
    assert decision.flow is Flow.GREETING
    assert decision.source == "rules"
    assert llm.calls == 0


async def test_greeting_mid_conversation_demotes_to_generative() -> None:
    # A "hola" with history must NOT replay the canned greeting; it continues with
    # context via a generative flow (SPEC_UAT_remediation New#1c).
    decision = await Router(_StubLLM(Flow.UNKNOWN)).route("Hola!", is_first_turn=False)
    assert decision.flow is Flow.UNKNOWN


async def test_llm_greeting_classification_demoted_mid_conversation() -> None:
    # Even if the LLM classifier returns greeting mid-conversation, it is demoted.
    decision = await Router(_StubLLM(Flow.GREETING)).route("mmm hola de nuevo", is_first_turn=False)
    assert decision.flow is Flow.UNKNOWN


async def test_explicit_handoff_keyword() -> None:
    decision = await Router(_StubLLM(Flow.UNKNOWN)).route(
        "quiero hablar con alguien", is_first_turn=False
    )
    assert decision.flow is Flow.HANDOFF
    assert decision.confidence >= 0.6


async def test_handoff_person_and_agent_keywords() -> None:
    # A6: "una persona" / "un agente" must hand off (previously fell to UNKNOWN).
    router = Router(_StubLLM(Flow.UNKNOWN))
    assert (
        await router.route("quiero hablar con una persona", is_first_turn=False)
    ).flow is Flow.HANDOFF
    assert (await router.route("me pasas con un agente?", is_first_turn=False)).flow is Flow.HANDOFF


async def test_handoff_expanded_keywords() -> None:
    # #78: ampliación del listado de handoff. Cada frase debe gatillar HANDOFF de forma
    # determinística (sin IA). "atencion"/"atención" sueltos + sinónimos de operador.
    llm = _StubLLM(Flow.UNKNOWN)
    router = Router(llm)
    phrases = (
        "pásame con alguien",
        "necesito atención",
        "esto es un reclamo",
        "quiero hablar con el operador",
        "me comunicas con el encargado?",
        "quiero un representante",
        "pásame con un supervisor",
        "tengo una queja",
    )
    for phrase in phrases:
        decision = await router.route(phrase, is_first_turn=False)
        assert decision.flow is Flow.HANDOFF, phrase
    assert llm.calls == 0  # todo resuelto por reglas, 0 IA


async def test_explicit_handoff_beats_first_turn_greeting() -> None:
    decision = await Router(_StubLLM(Flow.UNKNOWN)).route(
        "hola, quiero hablar con alguien", is_first_turn=True
    )
    assert decision.flow is Flow.HANDOFF


async def test_first_turn_service_intent_beats_greeting() -> None:
    # Leads from the ad campaign open with a specific course question on the FIRST
    # turn; a content-bearing intent must reach SERVICE (→ get_service) instead of
    # being swallowed by the canned catalog menu. Resolved by rules, 0 IA.
    llm = _StubLLM(Flow.UNKNOWN)
    router = Router(llm)
    phrases = (
        "Información del curso",
        "Informacion del curso por favor",
        "¿Cúanto cuesta el curso?",
    )
    for phrase in phrases:
        decision = await router.route(phrase, is_first_turn=True)
        assert decision.flow is Flow.SERVICE, phrase
        assert decision.source == "rules", phrase
    assert llm.calls == 0


async def test_first_turn_event_intent_beats_greeting() -> None:
    # #294 (UAT 30/08): campaign leads open with the EVENT, not the course — "Hola
    # buenas! Vengo por el evento" matched _GREETING ("hola"/"buenas") and got the
    # canned menu, swallowing the stated intent. "evento"/"entrada" must route to
    # SERVICE by rules, 0 IA, even alongside a greeting word.
    llm = _StubLLM(Flow.UNKNOWN)
    router = Router(llm)
    phrases = (
        "Hola buenas! Vengo por el evento",
        "Busco entradas para el evento",
        "quiero entradas",
    )
    for phrase in phrases:
        decision = await router.route(phrase, is_first_turn=True)
        assert decision.flow is Flow.SERVICE, phrase
        assert decision.source == "rules", phrase
    assert llm.calls == 0


async def test_first_turn_qualify_intent_beats_greeting() -> None:
    decision = await Router(_StubLLM(Flow.UNKNOWN)).route(
        "quiero inscribirme al curso", is_first_turn=True
    )
    assert decision.flow is Flow.QUALIFY


async def test_first_turn_bare_greeting_still_menus() -> None:
    # Regression: a cold "hola" (or an unmatched first message) must still get the
    # catalog-menu greeting on first contact.
    router = Router(_StubLLM(Flow.UNKNOWN))
    assert (await router.route("hola", is_first_turn=True)).flow is Flow.GREETING
    assert (await router.route("buenas", is_first_turn=True)).flow is Flow.GREETING


async def test_service_and_qualify_keywords() -> None:
    router = Router(_StubLLM(Flow.UNKNOWN))
    assert (await router.route("cuanto cuesta?", is_first_turn=False)).flow is Flow.SERVICE
    assert (await router.route("quiero inscribirme", is_first_turn=False)).flow is Flow.QUALIFY


async def test_low_confidence_falls_back_to_llm() -> None:
    llm = _StubLLM(Flow.SERVICE, confidence=0.88)
    decision = await Router(llm).route("mmm y eso", is_first_turn=False)
    assert llm.calls == 1
    assert decision.source == "llm"
    assert decision.flow is Flow.SERVICE


async def test_high_confidence_rule_skips_llm() -> None:
    llm = _StubLLM(Flow.UNKNOWN)
    await Router(llm).route("cuanto cuesta el curso", is_first_turn=False)
    assert llm.calls == 0
