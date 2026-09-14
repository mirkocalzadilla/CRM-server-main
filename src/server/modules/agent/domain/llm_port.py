"""LLM runtime port: provider-agnostic contract for the agent loop (§6).

The adapter implements this; the loop and tools depend only on these types,
never on the Anthropic SDK. This is the migration seam — a future LangGraph
or another provider reuses the same port.
"""

from __future__ import annotations

import enum
from dataclasses import dataclass
from typing import Protocol


class Role(enum.StrEnum):
    USER = "user"
    ASSISTANT = "assistant"


class LLMError(Exception):
    """Provider-neutral failure of a completion call (§6).

    Adapters translate their SDK's exceptions into this so the orchestrator can
    react (safe handoff + CRM visibility) without importing any provider SDK.
    `category` is a code the CRM translates to copy (card_flags philosophy).
    """

    AUTH = "auth"  # invalid/expired credentials (401/403)
    RATE_LIMIT = "rate_limit"  # provider throttling (429)
    BAD_REQUEST = "bad_request"  # request the provider rejects (400) — config/contract
    PROVIDER = "provider"  # provider-side failure (5xx/overloaded)
    NETWORK = "network"  # connection/timeout errors
    INTERNAL = "internal"  # anything else (bug, unexpected SDK behavior)

    def __init__(self, message: str, *, provider: str, category: str) -> None:
        super().__init__(message)
        self.provider = provider
        self.category = category


@dataclass(frozen=True, slots=True)
class ToolUse:
    """A tool invocation requested by the model."""

    id: str
    name: str
    input: dict[str, object]


@dataclass(frozen=True, slots=True)
class ToolResult:
    """A tool's output, fed back to the model on the next user turn."""

    tool_use_id: str
    content: str
    is_error: bool = False


@dataclass(frozen=True, slots=True)
class Message:
    """One conversation turn. Assistant turns carry `text` and/or `tool_uses`;
    user turns carry `text` or `tool_results`.
    """

    role: Role
    text: str = ""
    tool_uses: tuple[ToolUse, ...] = ()
    tool_results: tuple[ToolResult, ...] = ()


@dataclass(frozen=True, slots=True)
class ToolSpec:
    """Tool schema as the model sees it — no handler (§8)."""

    name: str
    description: str
    input_schema: dict[str, object]


@dataclass(frozen=True, slots=True)
class Turn:
    """Result of one completion: assistant text, requested tools, stop reason."""

    text: str
    tool_uses: tuple[ToolUse, ...]
    stop_reason: str


class LLMPort(Protocol):
    """A single LLM completion. Implemented by AnthropicAdapter.

    `temperature=None` keeps the provider's default; the agent loop forwards the
    value from the active agent's config snapshot (M-Config).
    """

    async def complete(
        self,
        *,
        system: str,
        messages: list[Message],
        tools: list[ToolSpec],
        temperature: float | None = None,
    ) -> Turn: ...
