"""Tests del AgentService: flujo + transición de funnel + handoff/silencio,
con store/sender/LLM stubbeados (sin DB, sin WhatsApp, sin LLM real)."""

from __future__ import annotations

import uuid
from collections.abc import Sequence
from dataclasses import dataclass, field

from server.modules.agent.domain.agent_state import State
from server.modules.agent.domain.funnel_fsm import FunnelStage
from server.modules.agent.domain.llm_port import (
    LLMError,
    Message,
    Role,
    ToolSpec,
    ToolUse,
    Turn,
)
from server.modules.agent.domain.ports import ConversationSnapshot, OutboundMedia
from server.modules.agent.domain.tool_catalogue import build_default_registry
from server.modules.agent.services.agent_service import ERROR_HANDOFF_REPLY, AgentService

_TENANT = uuid.uuid4()
_CONV = uuid.uuid4()
_CONFIG: dict[str, object] = {"services": {"cursos": {"precio": "480 Bs"}}}


@dataclass
class _SaveCall:
    reply_text: str
    funnel_stage: FunnelStage
    is_ai_active: bool
    handoff_reason: str | None
    answered_order: int = 0
    media: tuple[OutboundMedia, ...] = ()


def _last_user_order(window: tuple[Message, ...]) -> int | None:
    """Stub proxy for `latest_user_order`: 1-based index of the last lead turn, monotonic
    like the real `message_order` and enough to exercise the high-water-mark guard."""
    positions = [i for i, msg in enumerate(window, start=1) if msg.role is Role.USER]
    return positions[-1] if positions else None


class StubStore:
    def __init__(
        self,
        *,
        stage: FunnelStage = FunnelStage.NEW,
        is_ai_active: bool = True,
        window: tuple[Message, ...] = (),
        config: dict[str, object] | None = None,
        full_name: str | None = None,
        answered_through_order: int = 0,
        lead_turns_since_summary: int = 0,
    ) -> None:
        self._stage = stage
        self._is_ai_active = is_ai_active
        self._window = window
        self._config = config if config is not None else _CONFIG
        self._full_name = full_name
        self._answered_through_order = answered_through_order
        self._lead_turns_since_summary = lead_turns_since_summary
        self.saves: list[_SaveCall] = []
        self.full_names: list[str] = []  # nombres persistidos vía save_full_name (#91)
        self.agent_errors: list[tuple[str, str]] = []  # (category, detail) — server#288

    async def load(
        self, conversation_id: uuid.UUID, tenant_id: uuid.UUID
    ) -> ConversationSnapshot | None:
        return ConversationSnapshot(
            external_id="+59170000000",
            agent_id=uuid.uuid4(),
            funnel_stage=self._stage,
            is_ai_active=self._is_ai_active,
            system_prompt="Sos el asistente de Mirko.",
            config=self._config,
            messages_window=self._window,
            full_name=self._full_name,
            latest_user_order=_last_user_order(self._window),
            answered_through_order=self._answered_through_order,
            lead_turns_since_summary=self._lead_turns_since_summary,
        )

    async def save_turn(
        self,
        conversation_id: uuid.UUID,
        tenant_id: uuid.UUID,
        *,
        reply_text: str,
        funnel_stage: FunnelStage,
        is_ai_active: bool,
        answered_order: int,
        handoff_reason: str | None = None,
        media: Sequence[OutboundMedia] = (),
    ) -> None:
        self.saves.append(
            _SaveCall(
                reply_text, funnel_stage, is_ai_active, handoff_reason, answered_order, tuple(media)
            )
        )

    async def save_full_name(
        self, conversation_id: uuid.UUID, tenant_id: uuid.UUID, name: str
    ) -> None:
        self.full_names.append(name)

    async def save_agent_error(
        self, conversation_id: uuid.UUID, tenant_id: uuid.UUID, *, category: str, detail: str
    ) -> None:
        self.agent_errors.append((category, detail))


class StubSender:
    def __init__(self) -> None:
        self.sent: list[tuple[str, str]] = []
        self.documents: list[tuple[str, str, str, str]] = []  # (to, link, filename, caption)
        self.images: list[tuple[str, str, str]] = []  # (to, link, caption)

    async def send_text(self, to: str, body: str) -> None:
        self.sent.append((to, body))

    async def send_image(self, to: str, link: str, caption: str = "") -> None:
        self.images.append((to, link, caption))

    async def send_document(self, to: str, link: str, filename: str, caption: str = "") -> None:
        self.documents.append((to, link, filename, caption))


@dataclass
class StubHandoff:
    calls: list[tuple[str, str]] = field(default_factory=list)  # (reason, reply)

    async def on_handoff(self, state: object, *, reason: str, reply: str) -> None:
        self.calls.append((reason, reply))


@dataclass
class StubSummary:
    calls: list[str] = field(default_factory=list)  # reply por refresh
    orders: list[int] = field(default_factory=list)  # through_order por refresh (#254)

    async def refresh(self, state: object, *, reply: str, through_order: int) -> None:
        self.calls.append(reply)
        self.orders.append(through_order)


@dataclass
class StubLLM:
    loop_turns: list[Turn] = field(default_factory=list)
    systems_seen: list[str] = field(default_factory=list)
    temperatures_seen: list[float | None] = field(default_factory=list)
    tools_seen: list[int] = field(default_factory=list)  # tools ofrecidas por llamada (#313)
    messages_seen: list[list[Message]] = field(default_factory=list)  # ventana por llamada (#313)

    async def complete(
        self,
        *,
        system: str,
        messages: list[Message],
        tools: list[ToolSpec],
        temperature: float | None = None,
    ) -> Turn:
        self.systems_seen.append(system)
        self.temperatures_seen.append(temperature)
        self.tools_seen.append(len(tools))
        self.messages_seen.append(list(messages))
        if self.loop_turns:
            return self.loop_turns.pop(0)
        return Turn("ok", (), "end_turn")


def _use(name: str, payload: dict[str, object]) -> ToolUse:
    return ToolUse(id=f"t_{name}", name=name, input=payload)


@dataclass
class StubCapture:
    """Registra las llamadas del puerto de captura (#133)."""

    calls: list[tuple[uuid.UUID, tuple[str, ...]]] = field(default_factory=list)

    async def on_captured(self, state: State, slugs: tuple[str, ...]) -> None:
        self.calls.append((state.conversation_id, slugs))


def _orch(store: StubStore, llm: StubLLM, sender: StubSender) -> AgentService:
    return AgentService(store=store, llm=llm, registry=build_default_registry(), sender=sender)


def _inbound(text: str) -> tuple[Message, ...]:
    return (Message(role=Role.USER, text=text),)


async def test_greeting_advances_to_engaging_without_llm() -> None:
    store = StubStore(window=_inbound("hola"))
    llm, sender = StubLLM(), StubSender()
    await _orch(store, llm, sender).process_message(_CONV, _TENANT)
    assert store.saves[-1].funnel_stage is FunnelStage.ENGAGING
    assert sender.sent and "asistente de Mirko" in sender.sent[-1][1]
    assert sender.sent[-1][1].startswith(("Buenos días", "Buenas tardes", "Buenas noches"))
    assert llm.systems_seen == []  # deterministic: 0 IA


async def test_greeting_names_catalog_categories_in_prose() -> None:
    # #85+#265: con catálogo publicado, el saludo abre por franja horaria y nombra las
    # categorías (con ≥1 servicio) en una frase — nunca en lista con viñetas — cerrando
    # con el CTA de la variante C, todo determinístico (0 IA).
    config: dict[str, object] = {
        "services": [
            {"slug": "curso-edicion", "nombre": "Curso", "categoria": "Cursos"},
            {"slug": "prod-musical", "nombre": "Prod", "categoria": "Producción"},
            {"slug": "curso-color", "nombre": "Color", "categoria": "Cursos"},  # categoría dup
        ]
    }
    store = StubStore(stage=FunnelStage.NEW, window=_inbound("hola"), config=config)
    llm, sender = StubLLM(), StubSender()
    await _orch(store, llm, sender).process_message(_CONV, _TENANT)
    body = sender.sent[-1][1]
    assert body.startswith(("Buenos días", "Buenas tardes", "Buenas noches"))
    assert "😊" in body
    assert "Tenemos servicios de Cursos y también de Producción" in body
    assert body.count("Cursos") == 1  # no repite la categoría duplicada
    assert "Decime qué te llama la atención" in body
    assert "\n-" not in body  # sin lista con viñetas
    assert llm.systems_seen == []  # 0 IA


async def test_new_stage_with_history_does_not_replay_canned_greeting() -> None:
    # #77: a conversation whose funnel is NEW but that already has assistant history
    # (e.g. a reopened/reactivated lead) must NOT replay the canned greeting; it
    # continues generatively with context. Greeting is gated on a real first turn,
    # not solely on funnel_stage == NEW.
    window = (
        Message(role=Role.USER, text="hola, sigo interesado"),
        Message(role=Role.ASSISTANT, text="Son 480 Bs 🙂"),
        Message(role=Role.USER, text="hola"),
    )
    store = StubStore(stage=FunnelStage.NEW, window=window)
    llm = StubLLM(loop_turns=[Turn("Claro, seguimos! 🙂", (), "end_turn")])
    sender = StubSender()
    await _orch(store, llm, sender).process_message(_CONV, _TENANT)
    assert "asistente de Mirko" not in sender.sent[-1][1]  # not the canned greeting
    assert llm.systems_seen  # generative path ran (not the 0-IA greeting)


async def test_first_contact_without_history_still_greets() -> None:
    # Guardrail: a genuine first contact (NEW + no assistant history) still greets 0-IA.
    store = StubStore(stage=FunnelStage.NEW, window=_inbound("hola"))
    llm, sender = StubLLM(), StubSender()
    await _orch(store, llm, sender).process_message(_CONV, _TENANT)
    assert "asistente de Mirko" in sender.sent[-1][1]
    assert llm.systems_seen == []  # deterministic greeting, 0 IA


async def test_generative_service_runs_tool_and_replies() -> None:
    store = StubStore(stage=FunnelStage.ENGAGING, window=_inbound("cuanto cuesta?"))
    llm = StubLLM(
        loop_turns=[
            Turn("", (_use("get_service", {"linea": "cursos"}),), "tool_use"),
            Turn("Son 480 Bs 🙂", (), "end_turn"),
        ]
    )
    sender = StubSender()
    await _orch(store, llm, sender).process_message(_CONV, _TENANT)
    assert sender.sent[-1][1] == "Son 480 Bs 🙂"
    assert store.saves[-1].funnel_stage is FunnelStage.ENGAGING


async def test_generative_first_turn_injects_greeting_context() -> None:
    # #265: primer mensaje con consulta puntual (ruta generativa) → el system prompt
    # lleva el saludo por franja ya resuelto para que el modelo abra con él.
    store = StubStore(stage=FunnelStage.NEW, window=_inbound("cuanto cuesta el curso?"))
    llm = StubLLM(loop_turns=[Turn("Son 480 Bs 🙂", (), "end_turn")])
    sender = StubSender()
    await _orch(store, llm, sender).process_message(_CONV, _TENANT)
    assert "es el primer mensaje del lead" in llm.systems_seen[0]
    assert any(s in llm.systems_seen[0] for s in ("Buenos días", "Buenas tardes", "Buenas noches"))


async def test_generative_later_turn_has_no_greeting_context() -> None:
    # #265: pasado el primer turno, la línea de saludo no viaja más.
    store = StubStore(stage=FunnelStage.ENGAGING, window=_inbound("cuanto cuesta?"))
    llm, sender = StubLLM(), StubSender()
    await _orch(store, llm, sender).process_message(_CONV, _TENANT)
    assert "primer mensaje del lead" not in llm.systems_seen[0]


async def test_generative_passes_temperature_from_config() -> None:
    store = StubStore(
        stage=FunnelStage.ENGAGING,
        window=_inbound("cuanto cuesta?"),
        config={**_CONFIG, "temperature": 0.3},
    )
    llm, sender = StubLLM(), StubSender()
    await _orch(store, llm, sender).process_message(_CONV, _TENANT)
    assert llm.temperatures_seen == [0.3]


async def test_generative_without_temperature_uses_provider_default() -> None:
    store = StubStore(stage=FunnelStage.ENGAGING, window=_inbound("cuanto cuesta?"))
    llm, sender = StubLLM(), StubSender()
    await _orch(store, llm, sender).process_message(_CONV, _TENANT)
    assert llm.temperatures_seen == [None]


async def test_generative_first_turn_advances_new_to_engaging() -> None:
    # #310: un primer mensaje con intención ("cuanto cuesta el curso?") saltea el saludo
    # enlatado por precedencia del router — la card debe salir de "Nuevo" igual: entrar
    # al flujo generativo ES estar en conversación.
    store = StubStore(stage=FunnelStage.NEW, window=_inbound("cuanto cuesta el curso?"))
    llm = StubLLM(loop_turns=[Turn("Son 480 Bs 🙂", (), "end_turn")])
    sender = StubSender()
    await _orch(store, llm, sender).process_message(_CONV, _TENANT)
    assert store.saves[-1].funnel_stage is FunnelStage.ENGAGING


async def test_pre_tool_narration_dropped_from_reply() -> None:
    # #311: el texto que el modelo emite ANTES de llamar tools es narración (o un saludo
    # prematuro); concatenar las iteraciones del loop pegaba dos saludos en una misma
    # burbuja. Gana el último texto no vacío — nada se une.
    store = StubStore(stage=FunnelStage.NEW, window=_inbound("hablo por el curso de video"))
    final = "Buenas tardes, cómo estás? 😊 Soy el asistente de Mirko. El curso sale Bs 150."
    llm = StubLLM(
        loop_turns=[
            Turn(
                "Buenas tardes, cómo estás? 😊 Soy el asistente de Mirko.",
                (_use("get_service", {}),),
                "tool_use",
            ),
            Turn(final, (), "end_turn"),
        ]
    )
    sender = StubSender()
    await _orch(store, llm, sender).process_message(_CONV, _TENANT)
    body = sender.sent[-1][1]
    assert body == final
    assert body.count("Soy el asistente") == 1
    assert store.saves[-1].reply_text == final


async def test_silent_final_iteration_asks_for_reply_without_tools() -> None:
    # #313 (reemplaza el guard de #311 que reenviaba la narración): si el modelo habla
    # al llamar el tool y calla en la iteración final, la narración NO se envía; una
    # llamada extra SIN tools pide el mensaje real — el lead nunca queda sin mensaje.
    store = StubStore(stage=FunnelStage.ENGAGING, window=_inbound("cuanto cuesta?"))
    llm = StubLLM(
        loop_turns=[
            Turn("Te cuento 🙌", (_use("get_service", {"linea": "cursos"}),), "tool_use"),
            Turn("", (), "end_turn"),
            Turn("Son 480 Bs 🙂", (), "end_turn"),  # respuesta al nudge de finalize
        ]
    )
    sender = StubSender()
    await _orch(store, llm, sender).process_message(_CONV, _TENANT)
    assert sender.sent[-1][1] == "Son 480 Bs 🙂"
    assert "Te cuento 🙌" not in [body for _, body in sender.sent]
    assert llm.tools_seen[-1] == 0  # el nudge de finalize va sin tools
    # La iteración terminal MUDA no entra a la ventana: Anthropic rechaza un assistant
    # con content vacío en posición no-final y el 400 dejaría el nudge sin efecto.
    assert all(
        not (m.role is Role.ASSISTANT and not m.text and not m.tool_uses)
        for m in llm.messages_seen[-1]
    )


async def test_handoff_narration_never_reaches_lead() -> None:
    # #313 (UAT prod 2026-08-31): cierre consultivo — el modelo narró su plan interno
    # ("Veo que el servicio tiene flujo_cierre 'handoff_consultivo'. Procedo a guardar
    # el nombre y luego derivar.") junto a guardar_nombre + handoff_to_human, el loop
    # cortó ahí y esa narración con jerga interna le llegó al lead EN LUGAR de la
    # despedida. Ahora: narración descartada → fallback determinístico HANDOFF_REPLY.
    window = (
        Message(role=Role.USER, text="la 2da opcion me parece mejor"),
        Message(role=Role.ASSISTANT, text="Buenísimo! Me pasás tu nombre completo?"),
        Message(role=Role.USER, text="Ricardo Montaner Soliz"),
    )
    store = StubStore(stage=FunnelStage.QUALIFYING, window=window)
    narration = (
        "Veo que el servicio 'Solo fiesta' comercial tiene flujo_cierre "
        "'handoff_consultivo'. Procedo a guardar el nombre y luego derivar."
    )
    # El nombre pelado no matchea reglas → el clasificador del router consume el 1er turn.
    llm = StubLLM(
        loop_turns=[
            Turn(
                "", (_use("classify_intent", {"flow": "qualify", "confidence": 0.9}),), "tool_use"
            ),
            Turn(
                narration,
                (
                    _use("guardar_nombre", {"nombre": "Ricardo Montaner Soliz"}),
                    _use("handoff_to_human", {"motivo": "explicit_request"}),
                ),
                "tool_use",
            ),
        ]
    )
    sender = StubSender()
    await _orch(store, llm, sender).process_message(_CONV, _TENANT)
    save = store.saves[-1]
    assert save.handoff_reason == "explicit_request"
    assert save.is_ai_active is False
    assert save.reply_text == "Dale, te conecto con Mirko"
    assert sender.sent[-1][1] == "Dale, te conecto con Mirko"
    assert all("flujo_cierre" not in body for _, body in sender.sent)
    assert store.full_names == ["Ricardo Montaner Soliz"]  # el tool sí corrió


async def test_payment_handoff_narration_falls_back_to_proof_request() -> None:
    # #313: misma protección en el cierre de pago — narración junto al handoff
    # payment_validation → el lead recibe el pedido de comprobante (#89), no el plan.
    store = StubStore(
        stage=FunnelStage.QUALIFIED, window=_inbound("ya pague, aca esta el comprobante")
    )
    llm = StubLLM(
        loop_turns=[
            Turn(
                "El lead dice que pagó. Procedo a derivar para validar el pago.",
                (_use("handoff_to_human", {"motivo": "payment_validation"}),),
                "tool_use",
            ),
        ]
    )
    sender = StubSender()
    await _orch(store, llm, sender).process_message(_CONV, _TENANT)
    body = sender.sent[-1][1].lower()
    assert "comprobante" in body
    assert "procedo" not in body


async def test_unknown_service_narration_stays_silent() -> None:
    # #313: el silencio de diseño de unknown_service (#94) ya no depende de que el
    # modelo no narre — aunque narre junto al handoff, no se envía nada.
    store = StubStore(
        stage=FunnelStage.ENGAGING, window=_inbound("cuanto cuesta filmar un bautizo?")
    )
    llm = StubLLM(
        loop_turns=[
            Turn(
                "No encuentro ese servicio en el catálogo, derivo en silencio.",
                (_use("handoff_to_human", {"motivo": "unknown_service"}),),
                "tool_use",
            ),
        ]
    )
    sender = StubSender()
    await _orch(store, llm, sender).process_message(_CONV, _TENANT)
    assert store.saves[-1].reply_text == ""
    assert sender.sent == []


_CATALOG_CONFIG: dict[str, object] = {
    "services": [
        {
            "slug": "curso-edicion",
            "nombre": "Curso de edición",
            "precio": "480 Bs",
            "categoria": "Cursos",
            "categoria_slug": "cursos",
        },
        {
            "slug": "produccion-musical",
            "nombre": "Producción musical",
            "precio": "600 Bs",
            "categoria": "Cursos",
            "categoria_slug": "cursos",
        },
    ],
    "categories": [
        {
            "slug": "cursos",
            "nombre": "Cursos",
            "materials": [
                {"url": "https://media.test/curso-edicion.pdf", "filename": "curso-edicion.pdf"}
            ],
        },
    ],
}


async def test_accept_service_sends_correct_pdf_and_advances() -> None:
    # #235: al elegir una categoría, el bot envía el PDF de la categoría CORRECTA, avanza
    # de stage y NO emite la derivación genérica.
    store = StubStore(
        stage=FunnelStage.ENGAGING,
        window=_inbound("me interesan los cursos, mandame info"),
        config=_CATALOG_CONFIG,
    )
    llm = StubLLM(
        loop_turns=[
            Turn(
                "",
                (
                    _use("enviar_material", {"categoria_slug": "cursos"}),
                    _use("set_lead_stage", {"etapa": "qualifying", "razon": "interés"}),
                ),
                "tool_use",
            ),
            Turn(
                "Te paso la info del curso 🙌 cuando quieras avanzamos con la inscripción",
                (),
                "end_turn",
            ),
        ]
    )
    sender = StubSender()
    await _orch(store, llm, sender).process_message(_CONV, _TENANT)
    # T1: se envió el PDF del servicio correcto (no genérico).
    assert sender.documents, "no se despachó ningún documento"
    _, link, filename, _ = sender.documents[-1]
    assert filename == "curso-edicion.pdf"
    assert link == "https://media.test/curso-edicion.pdf"
    # T2: la card avanza de stage.
    assert store.saves[-1].funnel_stage is FunnelStage.QUALIFYING
    # T3: no aparece la derivación genérica en la aceptación.
    assert store.saves[-1].handoff_reason is None
    assert all("te conecto con mirko" not in body.lower() for _, body in sender.sent)
    # T4 (#175): el PDF enviado queda espejado en el hilo del CRM (media saliente en
    # save_turn) y el texto del turno también se persiste (el documento no lo suprime).
    media = store.saves[-1].media
    assert [m.media_type for m in media] == ["document"]
    assert media[0].url == "https://media.test/curso-edicion.pdf"
    assert media[0].filename == "curso-edicion.pdf"
    assert store.saves[-1].reply_text.startswith("Te paso la info")


async def test_decided_first_turn_climbs_to_qualified_via_tools() -> None:
    # #310: el lead decidido que acepta en su primer mensaje ya no queda clavado en NEW
    # (donde fijar_servicio era un no-op silencioso, #206): desde ENGAGING — emitido al
    # entrar al generativo — la escalera QUALIFYING→QUALIFIED corre entera en el turno.
    store = StubStore(
        stage=FunnelStage.NEW,
        window=_inbound("quiero inscribirme al curso de edicion"),
        config=_CATALOG_CONFIG,
    )
    llm = StubLLM(
        loop_turns=[
            Turn("", (_use("fijar_servicio", {"slug": "curso-edicion"}),), "tool_use"),
            Turn("Buenísimo! Te paso el QR 🙌", (), "end_turn"),
        ]
    )
    sender = StubSender()
    await _orch(store, llm, sender).process_message(_CONV, _TENANT)
    assert store.saves[-1].funnel_stage is FunnelStage.QUALIFIED


async def test_fijar_servicio_notifies_capture_port() -> None:
    # #133 v2: la captura se dispara con fijar_servicio (aceptación explícita del lead),
    # no con enviar_material. El slug validado se estampa en la card como source='captured'.
    store = StubStore(
        stage=FunnelStage.ENGAGING,
        window=_inbound("dale, quiero inscribirme"),  # 'inscrib' → QUALIFY (flujo generativo)
        config=_CATALOG_CONFIG,
    )
    llm = StubLLM(
        loop_turns=[
            Turn("", (_use("fijar_servicio", {"slug": "curso-edicion"}),), "tool_use"),
            Turn("Buenísimo, lo dejamos anotado 🙌", (), "end_turn"),
        ]
    )
    capture = StubCapture()
    service = AgentService(
        store=store,
        llm=llm,
        registry=build_default_registry(),
        sender=StubSender(),
        capture=capture,
    )
    await service.process_message(_CONV, _TENANT)
    assert capture.calls == [(_CONV, ("curso-edicion",))]


async def test_enviar_material_does_not_capture() -> None:
    # #133 v2: enviar_material solo manda el PDF de la categoría; ya no estampa el
    # servicio. La captura exige la aceptación explícita (fijar_servicio).
    store = StubStore(
        stage=FunnelStage.ENGAGING,
        window=_inbound("mandame info de los cursos"),
        config=_CATALOG_CONFIG,
    )
    llm = StubLLM(
        loop_turns=[
            Turn("", (_use("enviar_material", {"categoria_slug": "cursos"}),), "tool_use"),
            Turn("Te paso la info 🙌", (), "end_turn"),
        ]
    )
    capture = StubCapture()
    service = AgentService(
        store=store,
        llm=llm,
        registry=build_default_registry(),
        sender=StubSender(),
        capture=capture,
    )
    await service.process_message(_CONV, _TENANT)
    assert capture.calls == []


async def test_qualify_transition_via_tool() -> None:
    store = StubStore(stage=FunnelStage.QUALIFYING, window=_inbound("quiero inscribirme"))
    llm = StubLLM(
        loop_turns=[
            Turn("", (_use("set_lead_stage", {"etapa": "qualified", "razon": "x"}),), "tool_use"),
            Turn("Aca esta el QR 🙌", (), "end_turn"),
        ]
    )
    sender = StubSender()
    await _orch(store, llm, sender).process_message(_CONV, _TENANT)
    assert store.saves[-1].funnel_stage is FunnelStage.QUALIFIED


async def test_guardar_nombre_persists_full_name() -> None:
    # #91: el lead da su nombre al calificar → llega a conversation.full_name vía el
    # store, de donde el hook 'won' creará el contacto con ese nombre.
    store = StubStore(
        stage=FunnelStage.QUALIFYING, window=_inbound("quiero inscribirme, soy Juan Pérez")
    )
    llm = StubLLM(
        loop_turns=[
            Turn("", (_use("guardar_nombre", {"nombre": "Juan Pérez"}),), "tool_use"),
            Turn("Perfecto Juan! Te paso el QR 🙌", (), "end_turn"),
        ]
    )
    sender = StubSender()
    await _orch(store, llm, sender).process_message(_CONV, _TENANT)
    assert store.full_names == ["Juan Pérez"]


async def test_known_lead_name_injected_into_generative_system() -> None:
    # #91: si la conversación ya tiene full_name, se inyecta en el system del LLM para
    # personalizar la despedida/confirmación (robusto aunque se salga de la ventana).
    store = StubStore(
        stage=FunnelStage.QUALIFIED,
        window=_inbound("mandé el comprobante"),  # matchea _QUALIFY → generativo, sin clasificador
        full_name="Juan Pérez",
    )
    llm = StubLLM(loop_turns=[Turn("Gracias Juan! Te esperamos 🙌", (), "end_turn")])
    await _orch(store, llm, StubSender()).process_message(_CONV, _TENANT)
    assert any("el lead se llama Juan Pérez" in system for system in llm.systems_seen)


async def test_no_save_full_name_without_guardar_nombre() -> None:
    # Sin el tool guardar_nombre, no se toca conversation.full_name.
    store = StubStore(stage=FunnelStage.ENGAGING, window=_inbound("cuanto cuesta?"))
    llm = StubLLM(
        loop_turns=[
            Turn("", (_use("get_service", {"linea": "cursos"}),), "tool_use"),
            Turn("Son 480 Bs 🙂", (), "end_turn"),
        ]
    )
    await _orch(store, llm, StubSender()).process_message(_CONV, _TENANT)
    assert store.full_names == []


async def test_pago_qr_flow_sends_payment_qr_image() -> None:
    # Cierre 'pago_qr': el modelo califica y llama enviar_qr_pago → el lead recibe la
    # imagen del QR de pago con la URL configurada (PAYMENT_QR_URL inyectada).
    store = StubStore(stage=FunnelStage.QUALIFYING, window=_inbound("quiero inscribirme"))
    llm = StubLLM(
        loop_turns=[
            Turn(
                "",
                (
                    _use("set_lead_stage", {"etapa": "qualified", "razon": "quiere pagar"}),
                    _use("enviar_qr_pago", {}),
                ),
                "tool_use",
            ),
            Turn(
                "Buenísimo! Te paso el QR, cuando pagues mandame el comprobante 🙌", (), "end_turn"
            ),
        ]
    )
    sender = StubSender()
    service = AgentService(
        store=store,
        llm=llm,
        registry=build_default_registry(),
        sender=sender,
        payment_qr_url="https://qr.test/pago.jpg",
    )
    await service.process_message(_CONV, _TENANT)
    assert sender.images and sender.images[-1][1] == "https://qr.test/pago.jpg"
    assert store.saves[-1].funnel_stage is FunnelStage.QUALIFIED


async def test_pago_qr_sent_once_with_reply_as_caption() -> None:
    # #182: aunque el modelo pida el QR en dos iteraciones del loop, el lead recibe UNA
    # sola imagen y el texto del modelo viaja como caption (mismo mensaje), sin un texto
    # suelto aparte que lo duplique.
    store = StubStore(stage=FunnelStage.QUALIFYING, window=_inbound("quiero inscribirme"))
    llm = StubLLM(
        loop_turns=[
            Turn("", (_use("enviar_qr_pago", {}),), "tool_use"),
            Turn("", (_use("enviar_qr_pago", {}),), "tool_use"),
            Turn("Te paso el QR, cuando pagues mandame el comprobante 🙌", (), "end_turn"),
        ]
    )
    sender = StubSender()
    service = AgentService(
        store=store,
        llm=llm,
        registry=build_default_registry(),
        sender=sender,
        payment_qr_url="https://qr.test/pago.jpg",
    )
    await service.process_message(_CONV, _TENANT)
    assert len(sender.images) == 1  # una sola imagen pese a los dos enviar_qr_pago
    assert sender.images[0][2] == "Te paso el QR, cuando pagues mandame el comprobante 🙌"
    assert sender.sent == []  # el texto va como caption, no como mensaje suelto
    # #175: la imagen del QR queda espejada en el hilo con el reply como caption; el
    # texto no se persiste aparte (evita duplicar la burbuja en el CRM).
    media = store.saves[-1].media
    assert [m.media_type for m in media] == ["image"]
    assert media[0].url == "https://qr.test/pago.jpg"
    assert media[0].caption == "Te paso el QR, cuando pagues mandame el comprobante 🙌"
    assert store.saves[-1].reply_text == ""


async def test_receipt_turn_never_resends_payment_qr() -> None:
    # #315 (UAT prod 2026-08-31, hilo 4f652c1b): en el turno del comprobante el modelo
    # llamó enviar_qr_pago además del handoff → el acuse "Recibí tu comprobante!" salió
    # como caption del QR de pago reenviado, como si el lead debiera pagar de nuevo.
    # Un turno que deriva descarta el QR: el acuse sale como texto suelto.
    window = (
        Message(role=Role.USER, text="quiero pagar"),
        Message(
            role=Role.ASSISTANT,
            text="Te paso el QR. Cuando pagues, mandame el comprobante 🙌",
        ),
        Message(role=Role.USER, text="[image: sin descripción]"),
    )
    store = StubStore(stage=FunnelStage.QUALIFIED, window=window)
    # '[image: ...]' no matchea reglas → el clasificador del router consume el 1er turn.
    llm = StubLLM(
        loop_turns=[
            Turn(
                "", (_use("classify_intent", {"flow": "qualify", "confidence": 0.9}),), "tool_use"
            ),
            Turn(
                "",
                (
                    _use("enviar_qr_pago", {}),
                    _use("handoff_to_human", {"motivo": "payment_validation"}),
                ),
                "tool_use",
            ),
        ]
    )
    sender = StubSender()
    service = AgentService(
        store=store,
        llm=llm,
        registry=build_default_registry(),
        sender=sender,
        payment_qr_url="https://qr.test/pago.jpg",
    )
    await service.process_message(_CONV, _TENANT)
    save = store.saves[-1]
    assert save.handoff_reason == "payment_validation"
    assert sender.images == []  # el QR de pago NO se reenvía
    assert save.media == ()  # tampoco queda espejado en el hilo del CRM
    body = sender.sent[-1][1].lower()
    assert "recib" in body  # el acuse llega como texto suelto
    assert save.reply_text == sender.sent[-1][1]


async def test_paid_claim_with_spurious_qr_still_asks_proof_without_image() -> None:
    # #315: "ya pagué" por texto + enviar_qr_pago espurio junto al handoff → el lead
    # recibe el pedido de comprobante (#89) como texto, sin el QR de pago adjunto.
    store = StubStore(
        stage=FunnelStage.QUALIFIED, window=_inbound("ya pague, aca esta el comprobante")
    )
    llm = StubLLM(
        loop_turns=[
            Turn(
                "",
                (
                    _use("enviar_qr_pago", {}),
                    _use("handoff_to_human", {"motivo": "payment_validation"}),
                ),
                "tool_use",
            ),
        ]
    )
    sender = StubSender()
    service = AgentService(
        store=store,
        llm=llm,
        registry=build_default_registry(),
        sender=sender,
        payment_qr_url="https://qr.test/pago.jpg",
    )
    await service.process_message(_CONV, _TENANT)
    assert sender.images == []
    body = sender.sent[-1][1].lower()
    assert "comprobante" in body


async def test_handoff_turn_drops_payment_qr_queued_earlier_in_loop() -> None:
    # #315 alcance general: el QR encolado en una iteración anterior también se descarta
    # si el turno termina derivando — un turno que deriva jamás pide pagar.
    store = StubStore(stage=FunnelStage.QUALIFYING, window=_inbound("quiero inscribirme"))
    llm = StubLLM(
        loop_turns=[
            Turn("", (_use("enviar_qr_pago", {}),), "tool_use"),
            Turn("", (_use("handoff_to_human", {"motivo": "explicit_request"}),), "tool_use"),
        ]
    )
    sender = StubSender()
    service = AgentService(
        store=store,
        llm=llm,
        registry=build_default_registry(),
        sender=sender,
        payment_qr_url="https://qr.test/pago.jpg",
    )
    await service.process_message(_CONV, _TENANT)
    assert sender.images == []
    assert sender.sent[-1][1] == "Dale, te conecto con Mirko"


async def test_pago_qr_not_sent_when_url_unconfigured() -> None:
    # Sin PAYMENT_QR_URL (default ""), no se manda imagen aunque el modelo llame al tool.
    store = StubStore(stage=FunnelStage.QUALIFYING, window=_inbound("quiero inscribirme"))
    llm = StubLLM(loop_turns=[Turn("", (_use("enviar_qr_pago", {}),), "tool_use")])
    sender = StubSender()
    await _orch(store, llm, sender).process_message(_CONV, _TENANT)
    assert sender.images == []


async def test_payment_handoff_silences_agent() -> None:
    store = StubStore(stage=FunnelStage.QUALIFIED, window=_inbound("aca esta el comprobante"))
    llm = StubLLM(
        loop_turns=[
            Turn(
                "Listo 🙌",
                (_use("handoff_to_human", {"motivo": "payment_validation"}),),
                "tool_use",
            ),
        ]
    )
    sender = StubSender()
    await _orch(store, llm, sender).process_message(_CONV, _TENANT)
    save = store.saves[-1]
    assert save.funnel_stage is FunnelStage.HANDED_OFF
    assert save.is_ai_active is False
    assert save.handoff_reason == "payment_validation"


async def test_generative_handoff_without_text_still_replies() -> None:
    # Model calls handoff_to_human with no preamble text → the lead must still get a
    # message (HANDOFF_REPLY), not silence (A7 follow-up); it's stored and sent.
    store = StubStore(stage=FunnelStage.ENGAGING, window=_inbound("cuanto cuesta?"))
    llm = StubLLM(
        loop_turns=[
            Turn("", (_use("handoff_to_human", {"motivo": "explicit_request"}),), "tool_use")
        ]
    )
    sender = StubSender()
    await _orch(store, llm, sender).process_message(_CONV, _TENANT)
    save = store.saves[-1]
    assert save.funnel_stage is FunnelStage.HANDED_OFF
    assert save.is_ai_active is False
    assert save.handoff_reason == "explicit_request"
    assert save.reply_text == "Dale, te conecto con Mirko"
    assert sender.sent and sender.sent[-1][1] == "Dale, te conecto con Mirko"


async def test_unknown_service_hands_off_in_silence() -> None:
    # #94: el lead pide un servicio de Mirko que no figura en el catálogo. El modelo
    # deriva con motivo 'unknown_service' SIN texto → el agente queda en silencio (no
    # se completa con el genérico "te conecto con Mirko") y lo toma un humano.
    # "cuanto cuesta" matchea la regla SERVICE → ruteo generativo determinístico (sin que
    # el clasificador del router consuma el turno stub); el servicio pedido no está en el
    # catálogo, así que el modelo deriva con unknown_service.
    store = StubStore(stage=FunnelStage.ENGAGING, window=_inbound("cuanto cuesta filmar una boda?"))
    llm = StubLLM(
        loop_turns=[
            Turn("", (_use("handoff_to_human", {"motivo": "unknown_service"}),), "tool_use")
        ]
    )
    sender = StubSender()
    await _orch(store, llm, sender).process_message(_CONV, _TENANT)
    save = store.saves[-1]
    assert save.funnel_stage is FunnelStage.HANDED_OFF
    assert save.is_ai_active is False
    assert save.handoff_reason == "unknown_service"
    assert save.reply_text == ""  # silencio: sin texto del bot
    assert sender.sent == []  # nada enviado al lead


async def test_payment_declaration_asks_for_proof_not_generic_handoff() -> None:
    # #89: lead "ya pagué" → handoff payment_validation sin texto del modelo. El lead
    # debe recibir un pedido de comprobante, NO el genérico "te conecto con mirko".
    store = StubStore(
        stage=FunnelStage.QUALIFIED, window=_inbound("ya pague, te paso el comprobante")
    )
    llm = StubLLM(
        loop_turns=[
            Turn("", (_use("handoff_to_human", {"motivo": "payment_validation"}),), "tool_use")
        ]
    )
    sender = StubSender()
    await _orch(store, llm, sender).process_message(_CONV, _TENANT)
    save = store.saves[-1]
    assert save.funnel_stage is FunnelStage.HANDED_OFF  # → card "Por validar pago" vía motivo
    assert save.handoff_reason == "payment_validation"
    assert save.is_ai_active is False
    body = sender.sent[-1][1].lower()
    assert "comprobante" in body
    assert "te conecto con mirko" not in body


async def test_payment_proof_media_is_acknowledged_not_reasked() -> None:
    # e2e 2026-06-29: el lead MANDÓ el comprobante (imagen) → handoff payment_validation.
    # No hay que re-pedir lo que ya adjuntó: se acusa recibo. El webhook guarda la media
    # como '[image: ...]' (sin visión); `last_user_is_media` lo distingue del "ya pagué".
    window = (
        Message(role=Role.USER, text="quiero pagar"),
        Message(
            role=Role.ASSISTANT,
            text="Listo, te paso el QR. Cuando pagues, mandame el comprobante 🙌",
        ),
        Message(role=Role.USER, text="[image: sin descripción]"),
    )
    store = StubStore(stage=FunnelStage.QUALIFIED, window=window)
    # '[image: ...]' no matchea reglas → el router clasifica vía LLM (1er turn), después
    # el loop generativo deriva con payment_validation (2º turn).
    llm = StubLLM(
        loop_turns=[
            Turn(
                "", (_use("classify_intent", {"flow": "qualify", "confidence": 0.9}),), "tool_use"
            ),
            Turn("", (_use("handoff_to_human", {"motivo": "payment_validation"}),), "tool_use"),
        ]
    )
    sender = StubSender()
    await _orch(store, llm, sender).process_message(_CONV, _TENANT)
    save = store.saves[-1]
    assert save.handoff_reason == "payment_validation"
    assert save.is_ai_active is False
    body = sender.sent[-1][1].lower()
    assert "recib" in body  # acusa recibo del comprobante
    assert "mandame" not in body  # NO vuelve a pedir lo que ya mandó


async def test_explicit_handoff_is_deterministic() -> None:
    store = StubStore(stage=FunnelStage.ENGAGING, window=_inbound("quiero hablar con alguien"))
    llm, sender = StubLLM(), StubSender()
    await _orch(store, llm, sender).process_message(_CONV, _TENANT)
    save = store.saves[-1]
    assert save.funnel_stage is FunnelStage.HANDED_OFF
    assert save.is_ai_active is False
    assert save.handoff_reason == "explicit_request"
    assert llm.systems_seen == []  # 0 IA


async def test_explicit_handoff_derives_even_from_terminal_stage() -> None:
    # #84: un cierre legítimo (pide explícitamente un humano) SIEMPRE deriva a Gestión
    # Humana y silencia la IA, aún si el funnel ya está en un estado del que la FSM no
    # puede avanzar (p. ej. disqualified). Antes quedaba is_ai_active=True sin derivar.
    store = StubStore(
        stage=FunnelStage.DISQUALIFIED, window=_inbound("quiero hablar con alguien del equipo")
    )
    llm, sender = StubLLM(), StubSender()
    await _orch(store, llm, sender).process_message(_CONV, _TENANT)
    save = store.saves[-1]
    assert save.is_ai_active is False
    assert save.handoff_reason == "explicit_request"


async def test_silenced_agent_does_not_respond() -> None:
    store = StubStore(is_ai_active=False, window=_inbound("hola?"))
    llm, sender = StubLLM(), StubSender()
    await _orch(store, llm, sender).process_message(_CONV, _TENANT)
    assert store.saves == []
    assert sender.sent == []


async def test_already_answered_turn_is_skipped() -> None:
    # #187/#240: dispatch duplicado (p. ej. el catch-up re-encola una conversación cuyo
    # item vivo seguía en la cola). El inbound del lead ya está en el high-water mark
    # (`answered_through_order`), así que no hay nada nuevo que contestar → no re-responde
    # ni persiste, así nunca duplica la respuesta.
    window = (
        Message(role=Role.USER, text="cuanto cuesta?"),
        Message(role=Role.ASSISTANT, text="Son 480 Bs 🙂"),
    )
    store = StubStore(stage=FunnelStage.ENGAGING, window=window, answered_through_order=1)
    llm, sender = StubLLM(), StubSender()
    await _orch(store, llm, sender).process_message(_CONV, _TENANT)
    assert store.saves == []
    assert sender.sent == []
    assert llm.systems_seen == []  # 0 IA: ni siquiera entra al router/loop


async def test_follow_up_arriving_mid_turn_is_answered() -> None:
    # #240: el lead volvió a escribir mientras el turno anterior seguía en vuelo. La
    # ventana (ya cortada por el store) termina en el inbound nuevo, todavía sin
    # responder; su orden supera el high-water mark → se contesta. Antes el guard
    # posicional lo tragaba al ver la respuesta previa como "última fila".
    window = (
        Message(role=Role.USER, text="cuando inicia?"),  # ya respondido (mark = 1)
        Message(role=Role.USER, text="cuanto cuesta?"),  # llegó con el turno anterior en vuelo
    )
    store = StubStore(stage=FunnelStage.ENGAGING, window=window, answered_through_order=1)
    llm = StubLLM(loop_turns=[Turn("Cuesta 480 Bs 🙂", (), "end_turn")])
    sender = StubSender()
    await _orch(store, llm, sender).process_message(_CONV, _TENANT)
    assert sender.sent and sender.sent[-1][1] == "Cuesta 480 Bs 🙂"
    assert store.saves[-1].answered_order == 2  # el mark avanza al inbound nuevo


async def test_empty_window_is_skipped() -> None:
    # Sin turnos proyectables (window vacío tras el trim) no hay inbound que atender.
    store = StubStore(stage=FunnelStage.ENGAGING, window=())
    llm, sender = StubLLM(), StubSender()
    await _orch(store, llm, sender).process_message(_CONV, _TENANT)
    assert store.saves == []
    assert sender.sent == []


async def test_max_iterations_forces_agent_error_handoff() -> None:
    # Model keeps calling a tool forever → loop never converges.
    looping = [Turn("", (_use("get_service", {"linea": "cursos"}),), "tool_use") for _ in range(20)]
    store = StubStore(stage=FunnelStage.ENGAGING, window=_inbound("cuanto cuesta?"))
    llm = StubLLM(loop_turns=looping)
    sender = StubSender()
    await _orch(store, llm, sender).process_message(_CONV, _TENANT)
    save = store.saves[-1]
    assert save.funnel_stage is FunnelStage.HANDED_OFF
    assert save.handoff_reason == "agent_error"
    assert save.is_ai_active is False


async def test_unknown_tool_does_not_strand_the_lead() -> None:
    # The model hallucinates a tool name not in the registry. The loop must feed the
    # error back and recover (final text replied), never raise out and drop the turn.
    store = StubStore(stage=FunnelStage.ENGAGING, window=_inbound("cuanto cuesta?"))
    llm = StubLLM(
        loop_turns=[
            Turn("", (_use("nonexistent_tool", {"x": 1}),), "tool_use"),
            Turn("Son 480 Bs 🙂", (), "end_turn"),
        ]
    )
    sender = StubSender()
    await _orch(store, llm, sender).process_message(_CONV, _TENANT)
    assert sender.sent and sender.sent[-1][1] == "Son 480 Bs 🙂"
    assert store.saves[-1].funnel_stage is FunnelStage.ENGAGING


async def test_failing_tool_eventually_hands_off_instead_of_crashing() -> None:
    # A tool that keeps failing must not crash the worker: the loop exhausts and exits
    # via the agent_error safe handoff (lead never silently stranded).
    looping = [Turn("", (_use("nonexistent_tool", {}),), "tool_use") for _ in range(20)]
    store = StubStore(stage=FunnelStage.ENGAGING, window=_inbound("cuanto cuesta?"))
    await _orch(store, StubLLM(loop_turns=looping), StubSender()).process_message(_CONV, _TENANT)
    save = store.saves[-1]
    assert save.funnel_stage is FunnelStage.HANDED_OFF
    assert save.handoff_reason == "agent_error"
    assert save.is_ai_active is False


async def test_repeated_reply_is_regenerated_with_variation() -> None:
    # #88 refinado: el modelo vuelve a emitir EXACTO (normalizado) su último mensaje → en
    # vez de una línea fija, se le pide reformular y se envía la versión variada
    # (creatividad del LLM a la temperatura del agente), nunca el texto repetido.
    window = (
        Message(role=Role.USER, text="cuanto cuesta?"),
        Message(role=Role.ASSISTANT, text="Son 480 Bs 🙂"),
        # Ruta generativa (SERVICE); el guard es agnóstico al texto del lead.
        Message(role=Role.USER, text="sigo esperando, cuanto cuesta?"),
    )
    store = StubStore(stage=FunnelStage.ENGAGING, window=window)
    llm = StubLLM(
        loop_turns=[
            Turn("SON 480 BS 🙂.", (), "end_turn"),  # repite (normalizado) el anterior
            Turn("Cuesta 480 Bs, te sirve? 🙂", (), "end_turn"),  # reformulación
        ]
    )
    sender = StubSender()
    await _orch(store, llm, sender).process_message(_CONV, _TENANT)
    assert sender.sent[-1][1] == "Cuesta 480 Bs, te sirve? 🙂"
    assert store.saves[-1].reply_text == "Cuesta 480 Bs, te sirve? 🙂"  # inbox refleja lo enviado
    assert len(llm.systems_seen) == 2  # hubo una 2ª llamada al LLM: la reformulación


async def test_distinct_reply_passes_through_unchanged() -> None:
    # #88: si el reply es distinto del último mensaje del asistente, pasa tal cual (no
    # hay falsos positivos: solo se sustituye lo idéntico normalizado).
    window = (
        Message(role=Role.USER, text="cuanto cuesta?"),
        Message(role=Role.ASSISTANT, text="Son 480 Bs 🙂"),
        Message(role=Role.USER, text="cuanto cuesta el de producción?"),
    )
    store = StubStore(stage=FunnelStage.ENGAGING, window=window)
    llm = StubLLM(loop_turns=[Turn("Ese sale 600 Bs 🙂", (), "end_turn")])
    sender = StubSender()
    await _orch(store, llm, sender).process_message(_CONV, _TENANT)
    assert sender.sent[-1][1] == "Ese sale 600 Bs 🙂"


async def test_current_date_injected_into_generative_system() -> None:
    # Deterministic SERVICE route → systems_seen holds only the generative call (no classifier).
    store = StubStore(stage=FunnelStage.ENGAGING, window=_inbound("cuanto cuesta?"))
    llm = StubLLM(loop_turns=[Turn("480 Bs", (), "end_turn")])
    await _orch(store, llm, StubSender()).process_message(_CONV, _TENANT)
    gen_systems = [s for s in llm.systems_seen if "Sos el asistente de Mirko." in s]
    assert gen_systems  # editable persona preserved in the generative call
    assert "hoy es" in gen_systems[0]  # real date injected (not the model's training cutoff)


async def test_tenant_id_never_enters_llm_context() -> None:
    store = StubStore(stage=FunnelStage.ENGAGING, window=_inbound("cuanto cuesta?"))
    llm = StubLLM(loop_turns=[Turn("480 Bs", (), "end_turn")])
    await _orch(store, llm, StubSender()).process_message(_CONV, _TENANT)
    assert all(str(_TENANT) not in system for system in llm.systems_seen)


async def test_handoff_port_invoked_on_handoff() -> None:
    store = StubStore(stage=FunnelStage.ENGAGING, window=_inbound("quiero hablar con alguien"))
    handoff = StubHandoff()
    orch = AgentService(
        store=store,
        llm=StubLLM(),
        registry=build_default_registry(),
        sender=StubSender(),
        handoff=handoff,
    )
    await orch.process_message(_CONV, _TENANT)
    assert handoff.calls == [("explicit_request", "Dale, te conecto con Mirko")]


async def test_handoff_port_not_invoked_without_handoff() -> None:
    store = StubStore(window=_inbound("hola"))  # greeting → sin handoff
    handoff = StubHandoff()
    orch = AgentService(
        store=store,
        llm=StubLLM(),
        registry=build_default_registry(),
        sender=StubSender(),
        handoff=handoff,
    )
    await orch.process_message(_CONV, _TENANT)
    assert handoff.calls == []


async def test_summary_refreshed_on_stage_change() -> None:
    # Greeting avanza NEW→ENGAGING sin handoff → se refresca el resumen IA (#96).
    store = StubStore(window=_inbound("hola"))
    summary = StubSummary()
    orch = AgentService(
        store=store,
        llm=StubLLM(),
        registry=build_default_registry(),
        sender=StubSender(),
        summary=summary,
    )
    await orch.process_message(_CONV, _TENANT)
    assert len(summary.calls) == 1


async def test_summary_not_refreshed_on_handoff() -> None:
    # En handoff manda el resumen Sonnet del HandoffService, no el de transición.
    store = StubStore(stage=FunnelStage.ENGAGING, window=_inbound("quiero hablar con alguien"))
    summary, handoff = StubSummary(), StubHandoff()
    orch = AgentService(
        store=store,
        llm=StubLLM(),
        registry=build_default_registry(),
        sender=StubSender(),
        handoff=handoff,
        summary=summary,
    )
    await orch.process_message(_CONV, _TENANT)
    assert summary.calls == []
    assert handoff.calls  # el handoff sí corrió


def _service_turn_llm() -> StubLLM:
    # Turno generativo de servicio que NO cambia de etapa (se queda en ENGAGING).
    return StubLLM(
        loop_turns=[
            Turn("", (_use("get_service", {"linea": "cursos"}),), "tool_use"),
            Turn("Son 480 Bs 🙂", (), "end_turn"),
        ]
    )


async def test_summary_not_refreshed_below_throttle() -> None:
    # Mismo stage y aún por debajo del umbral (N=3, acá 2 turnos acumulados) → sin refresh.
    store = StubStore(
        stage=FunnelStage.ENGAGING, window=_inbound("cuanto cuesta?"), lead_turns_since_summary=2
    )
    summary = StubSummary()
    orch = AgentService(
        store=store,
        llm=_service_turn_llm(),
        registry=build_default_registry(),
        sender=StubSender(),
        summary=summary,
    )
    await orch.process_message(_CONV, _TENANT)
    assert summary.calls == []


@dataclass
class _ScriptedFlakyLLM:
    """Cada paso es un Turn a devolver o un LLMError a levantar (server#288)."""

    steps: list[Turn | LLMError] = field(default_factory=list)

    async def complete(
        self,
        *,
        system: str,
        messages: list[Message],
        tools: list[ToolSpec],
        temperature: float | None = None,
    ) -> Turn:
        step = self.steps.pop(0)
        if isinstance(step, LLMError):
            raise step
        return step


def _provider_error() -> LLMError:
    return LLMError("Anthropic caído", provider="anthropic", category=LLMError.PROVIDER)


async def test_llm_failure_hands_off_with_error_visibility() -> None:
    # server#288: el proveedor de IA falla el turno → el lead recibe el mensaje
    # determinístico de derivación (nunca silencio), la conversación pasa a Gestión
    # Humana con motivo agent_error y el hilo registra el evento de error (categoría).
    store = StubStore(stage=FunnelStage.ENGAGING, window=_inbound("cuanto cuesta?"))
    llm = _ScriptedFlakyLLM(steps=[_provider_error()])
    sender = StubSender()
    await _orch(store, llm, sender).process_message(_CONV, _TENANT)
    save = store.saves[-1]
    assert save.funnel_stage is FunnelStage.HANDED_OFF
    assert save.is_ai_active is False
    assert save.handoff_reason == "agent_error"
    assert save.reply_text == ERROR_HANDOFF_REPLY
    assert sender.sent and sender.sent[-1][1] == ERROR_HANDOFF_REPLY
    assert store.agent_errors == [("provider", "Anthropic caído")]


async def test_llm_failure_in_classifier_also_falls_back() -> None:
    # El fallo puede llegar desde el clasificador del router (capa 1, sub-umbral),
    # no solo desde el loop generativo: mismo fallback determinístico.
    store = StubStore(stage=FunnelStage.ENGAGING, window=_inbound("ok"))  # sin keywords
    llm = _ScriptedFlakyLLM(
        steps=[LLMError("credenciales inválidas", provider="anthropic", category=LLMError.AUTH)]
    )
    sender = StubSender()
    await _orch(store, llm, sender).process_message(_CONV, _TENANT)
    save = store.saves[-1]
    assert save.handoff_reason == "agent_error"
    assert save.is_ai_active is False
    assert store.agent_errors == [("auth", "credenciales inválidas")]


async def test_llm_failure_notifies_handoff_port() -> None:
    # La derivación por fallo del proveedor dispara el mismo side-effect que cualquier
    # handoff: handoff_event + notificación al inbox (HandoffService ya es best-effort
    # con su propio resumen, así que sobrevive al LLM caído).
    store = StubStore(stage=FunnelStage.ENGAGING, window=_inbound("cuanto cuesta?"))
    handoff = StubHandoff()
    orch = AgentService(
        store=store,
        llm=_ScriptedFlakyLLM(steps=[_provider_error()]),
        registry=build_default_registry(),
        sender=StubSender(),
        handoff=handoff,
    )
    await orch.process_message(_CONV, _TENANT)
    assert handoff.calls == [("agent_error", ERROR_HANDOFF_REPLY)]


async def test_vary_if_repeat_failure_keeps_original_reply() -> None:
    # #88 + server#288: si la reformulación anti-repetición falla en el proveedor, se
    # envía el reply original (best-effort) — jamás se pierde el turno por el retoque.
    window = (
        Message(role=Role.USER, text="cuanto cuesta?"),
        Message(role=Role.ASSISTANT, text="Son 480 Bs 🙂"),
        Message(role=Role.USER, text="sigo esperando, cuanto cuesta?"),
    )
    store = StubStore(stage=FunnelStage.ENGAGING, window=window)
    llm = _ScriptedFlakyLLM(
        steps=[Turn("SON 480 BS 🙂.", (), "end_turn"), _provider_error()]  # repite → reformular
    )
    sender = StubSender()
    await _orch(store, llm, sender).process_message(_CONV, _TENANT)
    assert sender.sent[-1][1] == "SON 480 BS 🙂."  # el original, no silencio
    assert store.saves[-1].handoff_reason is None  # el turno NO degeneró en handoff
    assert store.agent_errors == []  # tampoco se registró como fallo del turno


async def test_summary_refreshed_within_stage_after_n_turns() -> None:
    # Mismo stage pero ya se acumularon N=3 turnos del lead sobre el mark → refresh (#254),
    # avanzando el high-water mark hasta el último order del lead (throttle).
    store = StubStore(
        stage=FunnelStage.ENGAGING, window=_inbound("cuanto cuesta?"), lead_turns_since_summary=3
    )
    summary = StubSummary()
    orch = AgentService(
        store=store,
        llm=_service_turn_llm(),
        registry=build_default_registry(),
        sender=StubSender(),
        summary=summary,
    )
    await orch.process_message(_CONV, _TENANT)
    assert len(summary.calls) == 1
    assert summary.orders == [1]  # through_order = latest_user_order de la ventana
