"""Request-scoped agent state (§5.3).

A Pydantic object living in memory for the duration of one turn: who the agent is,
where the lead sits in the funnel, the detected flow, and the working-memory window.

**`tenant_id` is for query/Redis scoping only — it MUST NOT be put into the LLM
context** (prompt-injection vector). Handlers pass `system_prompt`/`config` to the
LLM, never this object.
"""

from __future__ import annotations

import uuid

from pydantic import BaseModel, ConfigDict

from server.modules.agent.domain.funnel_fsm import FunnelStage
from server.modules.agent.domain.llm_port import Message, Role


class State(BaseModel):
    """In-memory orchestration state for a single turn."""

    # arbitrary_types_allowed: Message is a frozen dataclass, kept opaque (no coercion).
    model_config = ConfigDict(arbitrary_types_allowed=True)

    tenant_id: uuid.UUID
    conversation_id: uuid.UUID
    agent_id: uuid.UUID
    external_id: str
    funnel_stage: FunnelStage
    is_ai_active: bool
    system_prompt: str
    config: dict[str, object]
    messages_window: tuple[Message, ...] = ()
    flow: str | None = None
    full_name: str | None = None  # lead's name if already captured (#91)

    @property
    def last_user_text(self) -> str:
        """Text of the latest user message in the window (the inbound turn)."""
        for message in reversed(self.messages_window):
            if message.role.value == "user" and message.text:
                return message.text
        return ""

    @property
    def last_user_is_media(self) -> bool:
        """True if the latest inbound was a media message (image/document). The webhook
        stores media as a '[image: ...]' / '[document: ...]' placeholder (no vision):
        lets the payment flow tell a sent comprobante (proof attached) apart from a mere
        'ya pagué' text (still needs to attach it)."""
        return self.last_user_text.startswith(("[image", "[document"))

    @property
    def temperature(self) -> float | None:
        """Sampling temperature from the agent's config snapshot; None = provider default."""
        value = self.config.get("temperature")
        if isinstance(value, bool) or not isinstance(value, int | float):
            return None
        return float(value)

    @property
    def is_first_turn(self) -> bool:
        """True only on a genuine first contact: funnel is NEW *and* there is no
        prior assistant turn in the loaded history. The greeting handler advances
        NEW→ENGAGING, so the stage gate alone normally suffices; the history check
        covers the case where a NEW-stage conversation still carries prior turns —
        a reopened/reactivated lead must continue with context, not replay the
        canned greeting (#77 / SPEC_UAT_remediation New#1c)."""
        if self.funnel_stage is not FunnelStage.NEW:
            return False
        return not any(message.role is Role.ASSISTANT for message in self.messages_window)
