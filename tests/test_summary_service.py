"""SummaryService (#96, #254): refresca `conversation.ai_summary` (Haiku), best-effort,
y avanza el high-water mark `summarized_through_order` del throttle.

Si el LLM falla, no pisa un resumen previo con vacío, pero avanza el mark igual.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field

from server.modules.agent.domain.agent_state import State
from server.modules.agent.domain.funnel_fsm import FunnelStage
from server.modules.agent.domain.llm_port import Message, Role, ToolSpec, Turn
from server.modules.agent.domain.models import Conversation
from server.modules.agent.services.summary_service import SummaryService

_TENANT = uuid.uuid4()
_CONV = uuid.uuid4()


class _FakeResult:
    def __init__(self, value: object) -> None:
        self._value = value

    def scalar_one_or_none(self) -> object:
        return self._value


class FakeSession:
    def __init__(self, conversation: object | None = None) -> None:
        self.conversation = conversation
        self.commits = 0

    async def execute(self, statement: object) -> _FakeResult:
        return _FakeResult(self.conversation)

    async def commit(self) -> None:
        self.commits += 1


@dataclass
class FakeSummarizer:
    text: str = "Lead pregunta por el curso de edición; quedó en pensarlo."
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


def _state() -> State:
    return State(
        tenant_id=_TENANT,
        conversation_id=_CONV,
        agent_id=uuid.uuid4(),
        external_id="59170000000",
        funnel_stage=FunnelStage.QUALIFYING,
        is_ai_active=True,
        system_prompt="x",
        config={},
        messages_window=(Message(role=Role.USER, text="cuanto cuesta?"),),
    )


def _conversation() -> Conversation:
    return Conversation(
        instance_id=uuid.uuid4(),
        organization_id=_TENANT,
        external_id="59170000000",
        funnel_stage=FunnelStage.QUALIFYING,
    )


def _service(summarizer: FakeSummarizer, session: FakeSession) -> SummaryService:
    return SummaryService(session=session, summarizer=summarizer)  # type: ignore[arg-type]


async def test_refresh_sets_ai_summary_and_advances_mark() -> None:
    conv = _conversation()
    conv.summarized_through_order = 5
    session = FakeSession(conversation=conv)
    summ = FakeSummarizer()

    await _service(summ, session).refresh(_state(), reply="Son 480 Bs", through_order=12)

    assert conv.ai_summary == summ.text
    assert conv.summarized_through_order == 12  # mark del throttle avanzado (#254)
    assert session.commits == 1


async def test_summarizer_gets_the_transcript_as_one_user_message() -> None:
    # server#290: la ventana + la respuesta iban como turnos user/assistant, con la
    # respuesta del bot al final → prefill → Haiku continuaba el chat en vez de resumir.
    session = FakeSession(conversation=_conversation())
    summ = FakeSummarizer()

    await _service(summ, session).refresh(_state(), reply="Son 480 Bs", through_order=12)

    assert len(summ.received) == 1
    (request,) = summ.received
    assert request.role is Role.USER
    assert "Lead: cuanto cuesta?" in request.text
    assert "Asistente: Son 480 Bs" in request.text


async def test_refresh_best_effort_on_llm_failure_advances_mark() -> None:
    # El LLM falla → no pisa el resumen previo con vacío, pero avanza el mark igual: el
    # throttle cuenta intentos, no éxitos, para no re-disparar refresh cada turno (#254).
    conv = _conversation()
    conv.ai_summary = "resumen previo"
    conv.summarized_through_order = 5
    session = FakeSession(conversation=conv)

    await _service(FakeSummarizer(raises=True), session).refresh(
        _state(), reply="x", through_order=12
    )

    assert conv.ai_summary == "resumen previo"  # no pisa con vacío
    assert conv.summarized_through_order == 12  # pero el mark avanza
    assert session.commits == 1
