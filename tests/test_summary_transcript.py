"""Transcript handed to the summarizer (server#290).

The bug this pins: the summarizer used to receive the conversation as chat turns with the
agent's reply as a trailing `assistant` message — a prefill — and answered with the bot's
next line instead of a summary. The request must be a single `user` message.
"""

from __future__ import annotations

from server.modules.agent.domain.llm_port import Message, Role
from server.modules.agent.domain.summary_transcript import (
    ASSISTANT_LABEL,
    LEAD_LABEL,
    summary_request,
    transcript,
)


def test_transcript_labels_turns_and_appends_the_reply() -> None:
    window = (
        Message(role=Role.USER, text="cuanto cuesta el curso?"),
        Message(role=Role.ASSISTANT, text="Son 650 Bs 🙂"),
        Message(role=Role.USER, text="[image: sin descripción]"),
    )

    text = transcript(window, reply="Recibí tu comprobante! Lo validamos en un rato 🙌")

    assert text.splitlines() == [
        f"{LEAD_LABEL}: cuanto cuesta el curso?",
        f"{ASSISTANT_LABEL}: Son 650 Bs 🙂",
        f"{LEAD_LABEL}: [image: sin descripción]",
        f"{ASSISTANT_LABEL}: Recibí tu comprobante! Lo validamos en un rato 🙌",
    ]


def test_summary_request_is_a_single_user_message_never_a_trailing_assistant() -> None:
    window = (Message(role=Role.USER, text="hola"),)

    request = summary_request(window, reply="Buenas! Soy el asistente")

    assert request is not None
    assert request.role is Role.USER
    assert request.tool_uses == () and request.tool_results == ()
    assert "Lead: hola" in request.text
    assert "Asistente: Buenas! Soy el asistente" in request.text


def test_empty_turns_and_whitespace_are_dropped() -> None:
    window = (
        Message(role=Role.USER, text="  ya   pagué \n"),
        Message(role=Role.ASSISTANT, text=""),
        Message(role=Role.USER, tool_results=()),
    )

    assert transcript(window, reply="   ") == f"{LEAD_LABEL}: ya pagué"


def test_nothing_to_summarize_yields_no_request() -> None:
    assert summary_request((), reply="") is None
    assert summary_request((Message(role=Role.ASSISTANT, text=" "),), reply="") is None
