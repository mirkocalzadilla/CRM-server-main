"""Hand-coded orchestration (§5.1, §5.2).

`AgentService.process_message`: load context → route → handle → persist → send.
Deterministic flows (greeting, explicit handoff) answer from templates with 0 IA;
generative flows run a self-contained `tool_use` loop where the model picks a
tool, the registry executes it, and the FSM validates every funnel change (the
LLM proposes, the code validates). `tenant_id` never enters the LLM context.

Store + sender are injected ports (`ConversationStore`, `MessageSender`): the real
implementations (`ConversationStoreAdapter` over M0's repos, `WhatsAppSender`) are
wired in `dispatch_handler.build_agent_service`. The console stubs in the M4 smoke
satisfy the same contract.
"""

from __future__ import annotations

import json
import unicodedata
import uuid
from dataclasses import dataclass

import sentry_sdk

from server.modules.agent.domain.agent_state import State
from server.modules.agent.domain.funnel_fsm import (
    FunnelStage,
    InvalidTransitionError,
    validate_transition,
)
from server.modules.agent.domain.llm_port import (
    LLMError,
    LLMPort,
    Message,
    Role,
    ToolResult,
    ToolUse,
)
from server.modules.agent.domain.ports import (
    ConversationStore,
    HandoffPort,
    MessageSender,
    OutboundMedia,
    ServiceCapturePort,
    SummaryPort,
)
from server.modules.agent.domain.runtime_context import opening_greeting, with_runtime_context
from server.modules.agent.domain.tools import ToolContext, ToolRegistry
from server.modules.agent.services.router import Flow, Router
from server.shared.logger import get_logger

logger = get_logger(__name__)

# Primer contacto (#265): saludo por franja + cortesía rotativa (runtime_context) y
# cuerpo variante C con las categorías del catálogo en frase, nunca en lista.
GREETING_FALLBACK_BODY = (
    "Soy el asistente de Mirko. Contame qué estás buscando y te paso toda la info"
)
GREETING_BODY_INTRO = "Soy el asistente de Mirko. Tenemos servicios de"
GREETING_CTA = "Decime qué te llama la atención y te cuento los detalles 🙌"
HANDOFF_REPLY = "Dale, te conecto con Mirko"
PAYMENT_HANDOFF_REPLY = "Perfecto! Para validar tu pago, mandame la foto del comprobante 🙌"
# El lead YA mandó el comprobante (imagen/doc): acusar recibo, nunca re-pedirlo.
PAYMENT_RECEIVED_REPLY = "Recibí tu comprobante! Lo validamos y te confirmamos en un rato 🙌"
ERROR_HANDOFF_REPLY = "Te paso con alguien del equipo para ayudarte mejor"
PAYMENT_QR_CAPTION = "Escaneá el QR para pagar y mandame la foto del comprobante 🙌"
DEFAULT_MAX_ITERATIONS = 6
DEFAULT_SUMMARY_REFRESH_EVERY_N_TURNS = 3  # within-stage `ai_summary` refresh throttle (#254)


@dataclass(frozen=True, slots=True)
class _Document:
    """A material (PDF/image) the agent must send to the lead this turn."""

    link: str
    filename: str
    caption: str = ""


@dataclass(frozen=True, slots=True)
class _Outcome:
    """Result of a handler before persistence."""

    reply: str
    funnel_stage: FunnelStage
    is_ai_active: bool
    handoff_reason: str | None = None
    documents: tuple[_Document, ...] = ()
    captured_slugs: tuple[str, ...] = ()  # servicios que el bot envió este turno (#133)
    images: tuple[str, ...] = ()  # imágenes a enviar (ej. QR de pago, flujo pago_qr)
    full_name: str | None = None  # nombre del lead capturado este turno (#91)


class AgentService:
    """Orchestrates one inbound turn. Store + LLMPort + ToolRegistry + sender are
    injected (§5.2)."""

    def __init__(
        self,
        *,
        store: ConversationStore,
        llm: LLMPort,
        registry: ToolRegistry,
        sender: MessageSender,
        router: Router | None = None,
        handoff: HandoffPort | None = None,
        summary: SummaryPort | None = None,
        capture: ServiceCapturePort | None = None,
        payment_qr_url: str = "",
        max_iterations: int = DEFAULT_MAX_ITERATIONS,
        summary_refresh_every_n_turns: int = DEFAULT_SUMMARY_REFRESH_EVERY_N_TURNS,
    ) -> None:
        self._store = store
        self._llm = llm
        self._registry = registry
        self._sender = sender
        self._router = router or Router(llm)
        self._handoff = handoff
        self._summary = summary
        self._capture = capture
        self._payment_qr_url = payment_qr_url
        self._max_iterations = max_iterations
        self._summary_refresh_every_n_turns = summary_refresh_every_n_turns

    async def process_message(self, conversation_id: uuid.UUID, tenant_id: uuid.UUID) -> None:
        snapshot = await self._store.load(conversation_id, tenant_id)
        if snapshot is None or not snapshot.is_ai_active:
            return  # unknown conversation or agent silenced after handoff (§9)

        # Nada nuevo que contestar: todo inbound del lead en la ventana está en o por
        # debajo del high-water mark (dispatch duplicado — p. ej. el catch-up de #187
        # re-encolando una conversación cuyo item vivo seguía en la cola). A diferencia de
        # mirar "la última fila ya es nuestra", esto sigue siendo correcto cuando un
        # follow-up llega con el turno anterior en vuelo y la respuesta previa lo supera
        # por message_order (#240). El lock por conversación serializa los turnos, así que
        # el mark ya refleja lo procesado al llegar acá.
        latest_order = snapshot.latest_user_order
        if latest_order is None or latest_order <= snapshot.answered_through_order:
            return

        state = State(
            tenant_id=tenant_id,
            conversation_id=conversation_id,
            agent_id=snapshot.agent_id,
            external_id=snapshot.external_id,
            funnel_stage=snapshot.funnel_stage,
            is_ai_active=snapshot.is_ai_active,
            system_prompt=snapshot.system_prompt,
            config=dict(snapshot.config),
            messages_window=snapshot.messages_window,
            full_name=snapshot.full_name,
        )

        try:
            decision = await self._router.route(
                state.last_user_text, is_first_turn=state.is_first_turn
            )
            state.flow = decision.flow.value
            outcome = await self._handle(decision.flow, state)
        except LLMError as exc:
            # Provider failed the turn (outage/credentials/config — server#288): the
            # lead must not be stranded in silence nor the CRM look like all went well.
            outcome = await self._llm_failure_outcome(state, exc)

        # #88: el anti-repetición vive en el flujo generativo (`_generate` regenera con la
        # temperatura del agente si el modelo repite textual su mensaje anterior). Los
        # flujos determinísticos (saludo, handoff) no se tocan. Persistimos y enviamos el
        # mismo `reply` para que el inbox del operador refleje lo que recibió el lead.
        reply = outcome.reply

        # Media saliente que recibe el lead este turno (materiales del catálogo + QR).
        # Se arma una sola vez y alimenta tanto la persistencia (mirror del CRM, #175)
        # como el envío a Meta, para que el hilo del operador refleje exactamente lo
        # enviado. Imágenes deduplicadas; el `reply` viaja como caption de la 1ª imagen.
        media: list[OutboundMedia] = [
            OutboundMedia("document", doc.link, doc.caption, doc.filename)
            for doc in outcome.documents
        ]
        image_caption = reply or PAYMENT_QR_CAPTION
        seen_images: set[str] = set()
        for image in outcome.images:
            if image in seen_images:
                continue
            seen_images.add(image)
            media.append(OutboundMedia("image", image, image_caption))
            image_caption = ""  # el caption va solo en la primera imagen
        # Con imágenes el texto viaja como su caption (no como fila de texto suelta),
        # igual que en el envío (#182): no lo persistimos ni lo mandamos por separado.
        text_reply = "" if outcome.images else reply

        await self._store.save_turn(
            conversation_id,
            tenant_id,
            reply_text=text_reply,
            funnel_stage=outcome.funnel_stage,
            is_ai_active=outcome.is_ai_active,
            answered_order=latest_order,
            handoff_reason=outcome.handoff_reason,
            media=tuple(media),
        )
        if outcome.full_name:
            # El lead dio su nombre → lo persistimos en conversation.full_name; el hook
            # 'won' (#101) crea el contacto con ese nombre al cerrar la oportunidad (#91).
            await self._store.save_full_name(conversation_id, tenant_id, outcome.full_name)
        if outcome.handoff_reason is not None and self._handoff is not None:
            await self._handoff.on_handoff(state, reason=outcome.handoff_reason, reply=reply)
        elif self._summary is not None:
            # Refrescamos el resumen IA (#96) al avanzar de etapa, o por acumulación de
            # turnos del lead dentro de la misma etapa (#254): un lead que se calienta sin
            # saltar de etapa ya no queda con un resumen congelado. El throttle
            # (`summarized_through_order`, avanzado por el refresh) evita regenerar por turno.
            stage_changed = outcome.funnel_stage is not state.funnel_stage
            accumulated = snapshot.lead_turns_since_summary >= self._summary_refresh_every_n_turns
            if stage_changed or accumulated:
                await self._summary.refresh(state, reply=reply, through_order=latest_order)
        # Se envía en el mismo orden en que se persistió: texto, luego documentos e
        # imágenes (ya deduplicadas, con el caption en la primera imagen) desde `media`.
        if text_reply:
            await self._sender.send_text(state.external_id, text_reply)
        for item in media:
            if item.media_type == "document":
                await self._sender.send_document(
                    state.external_id, item.url, item.filename, item.caption
                )
            else:
                await self._sender.send_image(state.external_id, item.url, item.caption)
        # El lead eligió un servicio (el bot le mandó su material) → lo estampamos en la
        # card como `captured` (#133). Después de enviar: registrar no debe demorar al lead.
        if self._capture is not None and outcome.captured_slugs:
            await self._capture.on_captured(state, outcome.captured_slugs)

    async def _llm_failure_outcome(self, state: State, exc: LLMError) -> _Outcome:
        """Deterministic fallback when the LLM provider fails (server#288, FLUJO §1).

        Persists a `role: system` error event in the thread (visible in the CRM, out
        of the LLM window) and derives to Gestión Postventa with the agent_error reason:
        the lead gets `ERROR_HANDOFF_REPLY` (0 IA) instead of silence, the card lands
        in the human pipeline and the 🔥/alert badges flag it. `is_ai_active=False`
        until the staff re-enables it once the provider recovers.
        """
        sentry_sdk.capture_exception(exc)
        logger.error(
            "agent.llm_error",
            conversation_id=str(state.conversation_id),
            provider=exc.provider,
            category=exc.category,
            error=str(exc),
        )
        try:
            await self._store.save_agent_error(
                state.conversation_id, state.tenant_id, category=exc.category, detail=str(exc)
            )
        except Exception as store_exc:  # visibility is best-effort; the handoff still runs
            logger.error(
                "agent.save_agent_error_failed",
                conversation_id=str(state.conversation_id),
                error=str(store_exc),
            )
        stage = _advance(state.funnel_stage, FunnelStage.HANDED_OFF)
        return _Outcome(
            ERROR_HANDOFF_REPLY, stage, is_ai_active=False, handoff_reason="agent_error"
        )

    async def _handle(self, flow: Flow, state: State) -> _Outcome:
        if flow is Flow.GREETING:
            stage = _advance(state.funnel_stage, FunnelStage.ENGAGING)
            return _Outcome(
                _greeting_reply(state.config, opening_greeting()), stage, is_ai_active=True
            )
        if flow is Flow.HANDOFF:
            # An explicit human request is a legitimate close: it ALWAYS derives to
            # Gestión Postventa and silences the agent — even when the FSM can't leave the
            # current stage (e.g. a disqualified-but-active lead). With is_ai_active=False
            # the card routing lands it in the human pipeline regardless of funnel (#84).
            stage = _advance(state.funnel_stage, FunnelStage.HANDED_OFF)
            return _Outcome(
                HANDOFF_REPLY, stage, is_ai_active=False, handoff_reason="explicit_request"
            )
        return await self._generate(state)  # SERVICE / FAQ / QUALIFY / UNKNOWN

    async def _generate(self, state: State) -> _Outcome:
        """Self-contained tool_use loop (§5.2). Bounded by `max_iterations`; on
        exhaustion it hands off with `agent_error` (FLUJO §1 fallback)."""
        messages = list(state.messages_window)
        specs = self._registry.specs()
        # Reaching the generative flow means the lead is in a live conversation, so
        # leaving NEW is a business fact — not a side effect of the canned greeting,
        # which an intent-bearing first message skips by router precedence, leaving
        # the card stuck in "Nuevo" until the payment handoff (#310). Only NEW moves;
        # the FSM keeps every other stage put.
        stage = _advance(state.funnel_stage, FunnelStage.ENGAGING)
        is_active = True
        handoff_reason: str | None = None
        reply_text = ""
        documents: list[_Document] = []
        captured_slugs: list[str] = []
        images: list[str] = []
        full_name: str | None = None
        system = with_runtime_context(
            state.system_prompt,
            lead_name=state.full_name,
            first_turn_greeting=opening_greeting() if state.is_first_turn else None,
        )
        for _ in range(self._max_iterations):
            turn = await self._llm.complete(
                system=system,
                messages=messages,
                tools=specs,
                temperature=state.temperature,
            )
            if not turn.tool_uses:
                # Only the terminal, tool-free turn talks to the lead. Text alongside
                # tool calls is internal narration — "veo que tiene flujo_cierre
                # 'handoff_consultivo', procedo a derivar" reached a lead in prod
                # despite the persona forbidding it (#313) — so it stays in `messages`
                # for the loop's context but is never sent nor persisted. A handoff
                # break below then leaves `reply` empty by design, landing on the
                # deterministic HANDOFF_/PAYMENT_HANDOFF_REPLY fallbacks.
                reply_text = turn.text
                if turn.text:
                    # An EMPTY terminal turn is not appended: Anthropic rejects empty
                    # assistant content in non-final position, which would 400 the
                    # _finalize_reply nudge that follows it — its one target scenario.
                    messages.append(Message(role=Role.ASSISTANT, text=turn.text, tool_uses=()))
                break
            messages.append(Message(role=Role.ASSISTANT, text=turn.text, tool_uses=turn.tool_uses))
            (
                results,
                stage,
                handoff_reason,
                is_active,
                turn_docs,
                turn_slugs,
                turn_images,
                turn_name,
            ) = await self._run_tools(
                turn.tool_uses, state.config, stage, is_active, handoff_reason
            )
            documents.extend(turn_docs)
            captured_slugs.extend(turn_slugs)
            images.extend(turn_images)
            if turn_name is not None:
                full_name = turn_name  # último nombre dado en el turno gana (#91)
            messages.append(Message(role=Role.USER, tool_results=tuple(results)))
            if handoff_reason is not None:
                break
        else:
            # Loop never converged → safe handoff so the lead is never stranded.
            stage = _advance(stage, FunnelStage.HANDED_OFF)
            return _Outcome(
                ERROR_HANDOFF_REPLY, stage, is_ai_active=False, handoff_reason="agent_error"
            )

        if handoff_reason is not None and images:
            # A turn that hands off never attaches the payment QR (#315): in prod the
            # model called enviar_qr_pago again alongside the receipt handoff, and the
            # acknowledgment went out as the caption of a re-sent payment QR — reading
            # as "pay again". The LLM proposes, the code validates: drop the image so
            # the deterministic ack/goodbye below ships as plain text.
            images = []
        reply = reply_text.strip()
        if handoff_reason == "payment_validation" and state.last_user_is_media:
            # El lead acaba de mandar el comprobante (imagen/doc): acusar recibo SIEMPRE,
            # nunca re-pedir lo que ya adjuntó (e2e 2026-06-29). Si en cambio sólo dijo
            # "ya pagué" por texto, cae en el fallback de abajo y se le pide la foto (#89).
            reply = PAYMENT_RECEIVED_REPLY
        elif not reply and handoff_reason and handoff_reason != "unknown_service":
            # Tool-driven handoff w/o model text: still tell the lead (A7). A payment
            # handoff asks for the proof image instead of the generic message (#89).
            # `unknown_service` is the exception: the lead asked for a service the agent
            # doesn't know, so it stays silent and a human takes over (#94) — no fallback.
            reply = (
                PAYMENT_HANDOFF_REPLY if handoff_reason == "payment_validation" else HANDOFF_REPLY
            )
        if handoff_reason is None:
            if not reply:
                # The terminal turn was silent (the model put everything in pre-tool
                # narration, which never reaches the lead, #313): ask once, without
                # tools, for the actual message — the lead is not left unanswered.
                reply = await self._finalize_reply(system, messages, state)
            reply = await self._vary_if_repeat(reply, system, messages, state)
        return _Outcome(
            reply,
            stage,
            is_ai_active=is_active,
            handoff_reason=handoff_reason,
            documents=tuple(documents),
            captured_slugs=tuple(captured_slugs),
            images=tuple(images),
            full_name=full_name,
        )

    async def _run_tools(
        self,
        tool_uses: tuple[ToolUse, ...],
        config: dict[str, object],
        stage: FunnelStage,
        is_active: bool,
        handoff_reason: str | None,
    ) -> tuple[
        list[ToolResult],
        FunnelStage,
        str | None,
        bool,
        list[_Document],
        list[str],
        list[str],
        str | None,
    ]:
        results: list[ToolResult] = []
        documents: list[_Document] = []
        captured: list[str] = []
        images: list[str] = []  # QR de pago u otras imágenes (cierre pago_qr)
        full_name: str | None = None  # nombre del lead, si lo dio este turno (#91)
        for use in tool_uses:
            ctx = ToolContext(funnel_stage=stage, config=config)
            try:
                result = await self._registry.execute(use.name, ctx, use.input)
            except Exception as exc:  # hallucinated tool / failing handler must not strand the lead
                err = json.dumps({"ok": False, "error": str(exc)})
                results.append(ToolResult(tool_use_id=use.id, content=err, is_error=True))
                continue
            results.append(ToolResult(tool_use_id=use.id, content=json.dumps(result)))
            if result.get("ok") and "etapa" in result:
                stage = FunnelStage(str(result["etapa"]))  # already FSM-validated by the tool
            docs = result.get("enviar_documentos")
            if result.get("ok") and isinstance(docs, list):
                for doc in docs:
                    if isinstance(doc, dict):
                        documents.append(
                            _Document(
                                link=str(doc.get("link", "")),
                                filename=str(doc.get("filename", "")),
                                caption=str(doc.get("caption", "")),
                            )
                        )
            if use.name == "fijar_servicio" and result.get("ok"):
                slug = str(result.get("slug", ""))
                if slug:
                    captured.append(slug)  # el lead ACEPTÓ este servicio (#133 v2)
            if result.get("ok") and result.get("enviar_qr_pago") and self._payment_qr_url:
                images.append(self._payment_qr_url)  # cierre pago_qr (#85 flujo)
            if use.name == "guardar_nombre" and result.get("ok"):
                name = result.get("full_name")
                if isinstance(name, str) and name:
                    full_name = name  # el lead dio su nombre (#91)
            if use.name == "handoff_to_human" and result.get("ok"):
                handoff_reason = str(result.get("motivo", "explicit_request"))
                is_active = False
        return results, stage, handoff_reason, is_active, documents, captured, images, full_name

    async def _finalize_reply(self, system: str, messages: list[Message], state: State) -> str:
        """#313: the tool loop ended without a user-facing text (the terminal turn was
        silent and pre-tool narration is never sent). One extra completion, without
        tools, asks for the message the lead should receive. Best-effort like
        `_vary_if_repeat`: on provider failure the turn ships only its media (if any)
        rather than leaking internal narration."""
        nudge = Message(
            role=Role.USER,
            text=(
                "No escribiste el mensaje para el lead: el texto que acompaña a tus "
                "herramientas es interno y no se envía. Escribí ahora SOLO ese mensaje, "
                "corto y natural, sin mencionar herramientas, campos internos ni pasos."
            ),
        )
        try:
            turn = await self._llm.complete(
                system=system,
                messages=[*messages, nudge],
                tools=[],
                temperature=state.temperature,
            )
        except LLMError as exc:
            logger.warning(
                "agent.finalize_reply_failed",
                conversation_id=str(state.conversation_id),
                error=str(exc),
            )
            return ""
        return turn.text.strip()

    async def _vary_if_repeat(
        self, reply: str, system: str, messages: list[Message], state: State
    ) -> str:
        """#88 (refinado): si el reply generado repite textual el último mensaje del
        asistente, pedirle al modelo que lo reformule a la temperatura del agente
        (variación creativa, NO una línea fija). Sólo corre en la ruta generativa: los
        flujos determinísticos (saludo, QR, handoff) quedan fuera por diseño."""
        previous = _last_assistant_text(state.messages_window)
        if not reply or not previous or _normalize(reply) != _normalize(previous):
            return reply
        nudge = Message(
            role=Role.USER,
            text=(
                "Acabás de repetir casi igual tu mensaje anterior. Reformulá: misma idea, "
                "otras palabras, breve y natural. No repitas el texto de antes."
            ),
        )
        try:
            turn = await self._llm.complete(
                system=system,
                messages=[*messages, nudge],
                tools=[],
                temperature=state.temperature,
            )
        except LLMError as exc:  # best-effort: a repeated reply beats losing the turn
            logger.warning(
                "agent.vary_if_repeat_failed",
                conversation_id=str(state.conversation_id),
                error=str(exc),
            )
            return reply
        return turn.text.strip() or reply


def _categories(config: dict[str, object]) -> list[str]:
    """Distinct service categories from the published catalog snapshot, in order. The
    snapshot only carries active services, so each category here has ≥1 available one (#85)."""
    services = config.get("services")
    if not isinstance(services, list):
        return []
    seen: list[str] = []
    for service in services:
        if isinstance(service, dict):
            categoria = service.get("categoria")
            if isinstance(categoria, str) and categoria and categoria not in seen:
                seen.append(categoria)
    return seen


def _greeting_reply(config: dict[str, object], greeting: str) -> str:
    """First contact (#265): time-of-day greeting + variant-C body naming the catalog
    categories (each with ≥1 service, #85) in prose — never a bulleted list, which
    reads as canned. Falls back to the generic CTA if the catalog isn't published."""
    categorias = _categories(config)
    if not categorias:
        return f"{greeting} {GREETING_FALLBACK_BODY}"
    return f"{greeting} {GREETING_BODY_INTRO} {_join_in_prose(categorias)}. {GREETING_CTA}"


def _join_in_prose(categorias: list[str]) -> str:
    """'A' | 'A y también de B' | 'A, B y también de C' (variant C wording, #265)."""
    if len(categorias) == 1:
        return categorias[0]
    return f"{', '.join(categorias[:-1])} y también de {categorias[-1]}"


def _normalize(text: str) -> str:
    """Normaliza para detectar repetición (#88): minúsculas, sin signos de puntuación
    (incluye ¡ ¿ ! ? . ,) y colapsando espacios. Dos mensajes que difieren solo en
    mayúsculas o puntuación comparan igual; los emojis y las letras se conservan."""
    folded = text.casefold()
    stripped = "".join(ch for ch in folded if not unicodedata.category(ch).startswith("P"))
    return " ".join(stripped.split())


def _last_assistant_text(window: tuple[Message, ...]) -> str:
    """Texto del último turno del asistente en la ventana (el mensaje anterior del bot)."""
    for message in reversed(window):
        if message.role is Role.ASSISTANT and message.text:
            return message.text
    return ""


def _advance(current: FunnelStage, target: FunnelStage) -> FunnelStage:
    """Apply a code-driven transition, keeping `current` if the FSM rejects it."""
    try:
        return validate_transition(current, target)
    except InvalidTransitionError:
        return current
