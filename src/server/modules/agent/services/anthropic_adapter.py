"""Anthropic Messages API adapter implementing `LLMPort` (§6).

Pinned model IDs (never `-latest`). Prompt caching (`cache_control: ephemeral`)
on system + tools to cut cost on the repeated prefix. The agent loop depends on
`LLMPort`, never on this module directly.

SDK v1 removed `temperature` from `messages.create()`; models that still accept
it API-side (≤4.6 line) get it via `extra_body`, the rest omit it. SDK failures
are translated into the port's `LLMError` so the orchestrator can react without
importing this SDK.
"""

from __future__ import annotations

from typing import Literal, cast

import anthropic
from anthropic import AsyncAnthropic
from anthropic.types import (
    CacheControlEphemeralParam,
    MessageParam,
    TextBlock,
    TextBlockParam,
    ToolParam,
    ToolUseBlock,
)
from anthropic.types import Message as AnthropicMessage

from server.modules.agent.domain.llm_port import LLMError, Message, Role, ToolSpec, ToolUse, Turn
from server.modules.agent.domain.model_catalog import model_supports_temperature
from server.shared.logger import get_logger

logger = get_logger(__name__)

_CACHE: CacheControlEphemeralParam = {"type": "ephemeral"}


class AnthropicAdapter:
    """Implements `LLMPort` over the Anthropic SDK."""

    def __init__(self, *, api_key: str, model: str, max_tokens: int = 1024) -> None:
        self._client = AsyncAnthropic(api_key=api_key)
        self._model = model
        self._max_tokens = max_tokens
        self._supports_temperature = model_supports_temperature(model)

    async def complete(
        self,
        *,
        system: str,
        messages: list[Message],
        tools: list[ToolSpec],
        temperature: float | None = None,
    ) -> Turn:
        request_temperature = temperature if self._supports_temperature else None
        try:
            try:
                return _to_turn(await self._create(system, messages, tools, request_temperature))
            except anthropic.BadRequestError as exc:
                if request_temperature is None or "temperature" not in str(exc).lower():
                    raise
                # The API says this model takes no temperature (the static gate was
                # wrong or the API changed): drop it for this process and retry once.
                self._supports_temperature = False
                logger.warning("llm.temperature_unsupported", model=self._model, error=str(exc))
                return _to_turn(await self._create(system, messages, tools, None))
        except Exception as exc:
            raise _to_llm_error(exc) from exc

    async def _create(
        self,
        system: str,
        messages: list[Message],
        tools: list[ToolSpec],
        temperature: float | None,
    ) -> AnthropicMessage:
        return await self._client.messages.create(
            model=self._model,
            max_tokens=self._max_tokens,
            system=_system_blocks(system),
            messages=[_message_param(m) for m in messages],
            tools=_tool_params(tools),
            # SDK v1 dropped the kwarg; models that accept it get it on the wire.
            extra_body={"temperature": temperature} if temperature is not None else None,
        )


def _to_llm_error(exc: Exception) -> LLMError:
    if isinstance(exc, anthropic.AuthenticationError | anthropic.PermissionDeniedError):
        category = LLMError.AUTH
    elif isinstance(exc, anthropic.RateLimitError):
        category = LLMError.RATE_LIMIT
    elif isinstance(exc, anthropic.BadRequestError):
        category = LLMError.BAD_REQUEST
    elif isinstance(exc, anthropic.APIStatusError):
        category = LLMError.PROVIDER  # 5xx / overloaded
    elif isinstance(exc, anthropic.APIConnectionError):  # includes APITimeoutError
        category = LLMError.NETWORK
    else:
        category = LLMError.INTERNAL
    return LLMError(str(exc), provider="anthropic", category=category)


def _system_blocks(system: str) -> list[TextBlockParam]:
    if not system:
        return []
    return [{"type": "text", "text": system, "cache_control": _CACHE}]


def _tool_params(tools: list[ToolSpec]) -> list[ToolParam]:
    params: list[ToolParam] = [
        cast(
            ToolParam,
            {"name": t.name, "description": t.description, "input_schema": t.input_schema},
        )
        for t in tools
    ]
    if params:
        params[-1]["cache_control"] = _CACHE  # caches the whole tool prefix
    return params


def _message_param(message: Message) -> MessageParam:
    role: Literal["user", "assistant"] = "assistant" if message.role is Role.ASSISTANT else "user"
    if message.tool_results:
        blocks = [
            {
                "type": "tool_result",
                "tool_use_id": r.tool_use_id,
                "content": r.content,
                "is_error": r.is_error,
            }
            for r in message.tool_results
        ]
        return cast(MessageParam, {"role": role, "content": blocks})
    if message.tool_uses:
        content: list[dict[str, object]] = []
        if message.text:
            content.append({"type": "text", "text": message.text})
        content.extend(
            {"type": "tool_use", "id": u.id, "name": u.name, "input": u.input}
            for u in message.tool_uses
        )
        return cast(MessageParam, {"role": role, "content": content})
    return cast(MessageParam, {"role": role, "content": message.text})


def _to_turn(response: AnthropicMessage) -> Turn:
    text_parts: list[str] = []
    tool_uses: list[ToolUse] = []
    for block in response.content:
        if isinstance(block, TextBlock):
            text_parts.append(block.text)
        elif isinstance(block, ToolUseBlock):
            tool_uses.append(ToolUse(id=block.id, name=block.name, input=block.input))
    return Turn(
        text="".join(text_parts),
        tool_uses=tuple(tool_uses),
        stop_reason=response.stop_reason or "end_turn",
    )
