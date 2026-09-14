"""The conversation as the summarizer reads it: one user message, not a chat to continue.

Both summaries (handoff and in-stage) used to hand the summarizer the lead/agent turns as
alternating `user`/`assistant` messages and append the agent's own reply as a trailing
`assistant` message. In the Messages API a trailing assistant message is a **prefill**:
the model continues that turn instead of following the system prompt, so the "summary"
came back as the bot's next line ("¿Alguna otra cosa que necesites?"). Rendering the
transcript inside a single user message removes the ambiguity — there is nothing to
continue, only something to summarize (server#290).
"""

from __future__ import annotations

from collections.abc import Sequence

from server.modules.agent.domain.llm_port import Message, Role

LEAD_LABEL = "Lead"
ASSISTANT_LABEL = "Asistente"
_REQUEST = "Resumí esta conversación de WhatsApp entre un lead y el asistente:"


def transcript(messages: Sequence[Message], reply: str = "") -> str:
    """Lead/agent turns as labeled lines, oldest first; `reply` is the agent's last line."""
    lines = [_line(message.role, message.text) for message in messages]
    lines.append(_line(Role.ASSISTANT, reply))
    return "\n".join(line for line in lines if line)


def summary_request(messages: Sequence[Message], reply: str = "") -> Message | None:
    """The single user message the summarizer receives, or None when there is nothing."""
    body = transcript(messages, reply)
    if not body:
        return None
    return Message(role=Role.USER, text=f"{_REQUEST}\n\n{body}")


def _line(role: Role, text: str) -> str:
    cleaned = " ".join(text.split())
    if not cleaned:
        return ""
    label = ASSISTANT_LABEL if role is Role.ASSISTANT else LEAD_LABEL
    return f"{label}: {cleaned}"
