"""Tests del HandoffService con fakes (sin DB/Redis/LLM real).

Verifica: escribe `handoff_event` con los campos del contrato, commitea, publica al
canal por tenant, y que el resumen es **best-effort** (si Sonnet falla → summary
vacío pero event + publish igual ocurren).
"""

from __future__ import annotations

import json
import uuid
from dataclasses import dataclass, field

from server.modules.agent.domain.agent_state import State
from server.modules.agent.domain.funnel_fsm import FunnelStage
from server.modules.agent.domain.llm_port import Message, Role, ToolSpec, Turn
from server.modules.agent.domain.models import Conversation, HandoffEvent
from server.modules.agent.services.handoff_service import HandoffService
from server.shared.pubsub import crm_channel

_TENANT = uuid.uuid4()
_CONV = uuid.uuid4()
_AGENT = uuid.uuid4()


class _FakeResult:
    def __init__(self, value: object) -> None:
        self._value = value

    def scalar_one_or_none(self) -> object:
        return self._value


class FakeSession:
    def __init__(self, conversation: object | None = None) -> None:
        self.added: list[object] = []
        self.commits = 0
        self._conversation = conversation  # lo que devuelve get_by_id (espejo de ai_summary)

    def add(self, obj: object) -> None:
        self.added.append(obj)

    async def flush(self) -> None:
        return None

    async def refresh(self, obj: object) -> None:
        return None

    async def execute(self, statement: object) -> _FakeResult:
        return _FakeResult(self._conversation)

    async def commit(self) -> None:
        self.commits += 1


@dataclass
class FakeSummarizer:
    text: str = "El lead quiere inscribirse; mandó comprobante. Derivado para validar pago."
    raises: bool = False
    received: list[Message] = field(default_factory=list)

    async def complete(
        self,
        *,
        system: str,
        messages: list[Message],
        tools: list[ToolSpec],
        temperature: float | None = None,
    ) -> Turn:
        self.received = list(messages)
        if self.raises:
            raise RuntimeError("sin saldo")
        return Turn(self.text, (), "end_turn")


@dataclass
class FakePublisher:
    published: list[tuple[str, dict[str, object]]] = field(default_factory=list)

    async def publish(self, channel: str, payload: dict[str, object]) -> None:
        self.published.append((channel, payload))


def _state() -> State:
    return State(
        tenant_id=_TENANT,
        conversation_id=_CONV,
        agent_id=_AGENT,
        external_id="59170000000",
        funnel_stage=FunnelStage.QUALIFIED,
        is_ai_active=False,
        system_prompt="x",
        config={},
        messages_window=(Message(role=Role.USER, text="ya pagué, aca el comprobante"),),
    )


def _service(
    summarizer: FakeSummarizer, session: FakeSession, publisher: FakePublisher
) -> HandoffService:
    return HandoffService(session=session, summarizer=summarizer, publisher=publisher)  # type: ignore[arg-type]


async def test_writes_event_commits_and_publishes() -> None:
    session, publisher = FakeSession(), FakePublisher()
    summ = FakeSummarizer()
    await _service(summ, session, publisher).on_handoff(
        _state(), reason="payment_validation", reply="Listo, el equipo valida 🙌"
    )

    events = [o for o in session.added if isinstance(o, HandoffEvent)]
    assert len(events) == 1
    ev = events[0]
    assert ev.agent_id == _AGENT
    assert ev.organization_id == _TENANT
    assert ev.thread_id == str(_CONV)
    assert ev.reason == "payment_validation"
    assert ev.context["summary"] == summ.text
    assert ev.context["from_stage"] == "qualified"
    assert session.commits == 1

    assert len(publisher.published) == 1
    channel, payload = publisher.published[0]
    assert channel == crm_channel(_TENANT)
    assert payload["type"] == "handoff"
    assert payload["conversation_id"] == str(_CONV)
    assert payload["reason"] == "payment_validation"
    assert payload["summary"] == summ.text
    json.dumps(payload)  # payload serializable


async def test_mirrors_summary_to_conversation_ai_summary() -> None:
    # El resumen del handoff también se persiste en conversation.ai_summary para el
    # read-model unificado del CRM (#96).
    conversation = Conversation(
        instance_id=uuid.uuid4(),
        organization_id=_TENANT,
        external_id="59170000000",
        funnel_stage=FunnelStage.QUALIFIED,
    )
    session, publisher = FakeSession(conversation=conversation), FakePublisher()
    summ = FakeSummarizer()

    await _service(summ, session, publisher).on_handoff(
        _state(), reason="payment_validation", reply="Listo"
    )

    assert conversation.ai_summary == summ.text


async def test_summarizer_gets_the_transcript_as_one_user_message() -> None:
    # server#290: con la respuesta del bot como último turno `assistant` el modelo la
    # continuaba ("¿Alguna otra cosa que necesites?") en vez de resumir la derivación.
    session, publisher = FakeSession(), FakePublisher()
    summ = FakeSummarizer()

    await _service(summ, session, publisher).on_handoff(
        _state(), reason="payment_validation", reply="Recibí tu comprobante! Lo validamos 🙌"
    )

    assert len(summ.received) == 1
    (request,) = summ.received
    assert request.role is Role.USER
    assert "Lead: ya pagué, aca el comprobante" in request.text
    assert "Asistente: Recibí tu comprobante! Lo validamos 🙌" in request.text


async def test_summary_best_effort_when_llm_fails() -> None:
    session, publisher = FakeSession(), FakePublisher()
    await _service(FakeSummarizer(raises=True), session, publisher).on_handoff(
        _state(), reason="explicit_request", reply="Te conecto con el equipo"
    )

    events = [o for o in session.added if isinstance(o, HandoffEvent)]
    assert len(events) == 1
    assert events[0].context["summary"] == ""  # resumen vacío, pero el evento existe
    assert session.commits == 1
    assert publisher.published[0][1]["summary"] == ""  # y se publica igual
