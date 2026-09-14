"""Guard del persona por defecto: que el baseline sembrado lleve las reglas duras
de FLUJO_AGENTE §3 (voseo, sin markdown, sin signos de apertura) y no vuelva al
one-liner genérico que sonaba a ChatGPT en la UAT."""

from __future__ import annotations

from server.modules.agent.domain.persona import DEFAULT_PERSONA_PROMPT


def test_persona_is_not_the_naked_one_liner() -> None:
    assert len(DEFAULT_PERSONA_PROMPT) > 200  # baseline con reglas, no una sola frase


def test_persona_has_voseo_and_informal_cue() -> None:
    assert "voseo" in DEFAULT_PERSONA_PROMPT.lower()


def test_persona_forbids_markdown() -> None:
    assert "markdown" in DEFAULT_PERSONA_PROMPT.lower()
    assert "**" in DEFAULT_PERSONA_PROMPT  # nombra explícitamente el símbolo a evitar


def test_persona_forbids_opening_marks() -> None:
    assert "¡" in DEFAULT_PERSONA_PROMPT and "¿" in DEFAULT_PERSONA_PROMPT


def test_persona_connects_lead_with_mirko() -> None:
    # Decisión 2026-06-28: de cara al lead se dice "con Mirko" (aunque atienda su equipo).
    assert "con Mirko" in DEFAULT_PERSONA_PROMPT


def test_persona_routes_unknown_service_to_silent_handoff() -> None:
    # #94: ante un servicio fuera del catálogo deriva con motivo unknown_service, sin
    # inventar ni mandar al lead a "contactar al equipo".
    assert "unknown_service" in DEFAULT_PERSONA_PROMPT


def test_persona_offers_categories_first() -> None:
    # e2e 2026-06-29 Issue B: el saludo ofrece CATEGORÍAS (no la lista plana de
    # servicios) e interpreta la intención amplia ("qué más tienen") listando todas.
    lower = DEFAULT_PERSONA_PROMPT.lower()
    assert "categor" in lower
    assert "enviar_material" in DEFAULT_PERSONA_PROMPT


def test_persona_answers_specific_first_turn_intent() -> None:
    # Leads de campaña abren con una consulta puntual del curso: la persona debe
    # responder eso directo (no el menú) y, si hay UN SOLO curso, dar su detalle.
    lower = DEFAULT_PERSONA_PROMPT.lower()
    assert "un solo curso" in lower
    assert "get_service" in DEFAULT_PERSONA_PROMPT


def test_persona_prequalifies_before_dumping_info() -> None:
    # #185: reduce el texto de entrada y agrega una capa de calificación previa —
    # segmenta con una pregunta corta antes de volcar precios/detalles.
    lower = DEFAULT_PERSONA_PROMPT.lower()
    assert "segment" in lower
    assert "no vuelques toda la info" in lower


def test_persona_fixes_service_on_acceptance() -> None:
    # #133 v2: el servicio se fija con fijar_servicio recién cuando el lead ACEPTA,
    # no ante una simple consulta.
    assert "fijar_servicio" in DEFAULT_PERSONA_PROMPT
    lower = DEFAULT_PERSONA_PROMPT.lower()
    assert "acepta" in lower


def test_persona_treats_short_affirmative_as_acceptance() -> None:
    # #294 (UAT 30/08): "Ese porfa" tras "Te interesa?" repetía los detalles y
    # re-preguntaba; una afirmación corta a una pregunta de oferta propia es aceptación
    # y no se encadenan dos confirmaciones seguidas.
    lower = DEFAULT_PERSONA_PROMPT.lower()
    assert "respuesta corta afirmativa" in lower
    assert "no encadenes dos confirmaciones" in lower


def test_persona_never_relists_given_data_and_hides_tool_use() -> None:
    # #294: backport del prompt vivo (v18) — las tools se consultan en silencio y los
    # datos ya dados (fecha, lugar, precio) no se vuelven a listar.
    lower = DEFAULT_PERSONA_PROMPT.lower()
    assert "nunca narres" in lower
    assert "no repitas datos que ya diste" in lower


def test_persona_sells_consultivo_before_handoff() -> None:
    # Regresión del bug: un servicio 'handoff_consultivo' se vende y califica igual;
    # el flujo_cierre no es motivo para derivar apenas lo mencionan.
    assert "handoff_consultivo" in DEFAULT_PERSONA_PROMPT
    lower = DEFAULT_PERSONA_PROMPT.lower()
    assert "no se deriva apenas" in lower
