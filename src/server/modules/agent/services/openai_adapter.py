"""OpenAI Chat Completions adapter implementing `LLMPort` (§6).

Alternate runtime, selectable via `LLM_PROVIDER=openai`; Anthropic stays the
default. Maps the neutral port contract to OpenAI function calling and back:
assistant `tool_uses` -> `tool_calls`, and a user turn's `tool_results` -> one
`role:tool` message each (OpenAI has no bundled tool turn). No prompt caching
(no direct equivalent). Pinned model IDs, never bare aliases. The agent loop
depends on `LLMPort`, never on this module directly.
"""

from __future__ import annotations

import json
from typing import cast

import openai
from openai import AsyncOpenAI, omit
from openai.types.chat import (
    ChatCompletion,
    ChatCompletionMessageParam,
    ChatCompletionToolParam,
)
from openai.types.chat.chat_completion_message_function_tool_call import (
    ChatCompletionMessageFunctionToolCall,
)

from server.modules.agent.domain.llm_port import LLMError, Message, Role, ToolSpec, ToolUse, Turn
from server.modules.agent.domain.model_catalog import model_supports_temperature
from server.shared.logger import get_logger

logger = get_logger(__name__)

# OpenAI finish_reason -> neutral stop_reason. Cosmetic: the loop branches on
# tool_uses, not on this.
_STOP_REASON: dict[str, str] = {
    "tool_calls": "tool_use",
    "stop": "end_turn",
    "length": "max_tokens",
}


class OpenAIAdapter:
    """Implements `LLMPort` over the OpenAI Chat Completions API."""

    def __init__(self, *, api_key: str, model: str, max_tokens: int = 1024) -> None:
        self._client = AsyncOpenAI(api_key=api_key)
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
            except openai.BadRequestError as exc:
                if request_temperature is None or "temperature" not in str(exc).lower():
                    raise
                # Same self-healing as the Anthropic adapter: the API refused the
                # sampling param for this model → drop it and retry once.
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
    ) -> ChatCompletion:
        tool_params = _tool_params(tools)
        return await self._client.chat.completions.create(
            model=self._model,
            max_completion_tokens=self._max_tokens,
            messages=_to_openai_messages(system, messages),
            tools=tool_params if tool_params else omit,
            temperature=temperature if temperature is not None else omit,
        )


def _to_llm_error(exc: Exception) -> LLMError:
    if isinstance(exc, openai.AuthenticationError | openai.PermissionDeniedError):
        category = LLMError.AUTH
    elif isinstance(exc, openai.RateLimitError):
        category = LLMError.RATE_LIMIT
    elif isinstance(exc, openai.BadRequestError):
        category = LLMError.BAD_REQUEST
    elif isinstance(exc, openai.APIStatusError):
        category = LLMError.PROVIDER  # 5xx
    elif isinstance(exc, openai.APIConnectionError):  # includes APITimeoutError
        category = LLMError.NETWORK
    else:
        category = LLMError.INTERNAL
    return LLMError(str(exc), provider="openai", category=category)


def _tool_params(tools: list[ToolSpec]) -> list[ChatCompletionToolParam]:
    return [
        cast(
            ChatCompletionToolParam,
            {
                "type": "function",
                "function": {
                    "name": t.name,
                    "description": t.description,
                    "parameters": t.input_schema,
                },
            },
        )
        for t in tools
    ]


def _to_openai_messages(system: str, messages: list[Message]) -> list[ChatCompletionMessageParam]:
    params: list[ChatCompletionMessageParam] = []
    if system:
        params.append(cast(ChatCompletionMessageParam, {"role": "system", "content": system}))
    for message in messages:
        params.extend(_message_params(message))
    return params


def _message_params(message: Message) -> list[ChatCompletionMessageParam]:
    if message.tool_results:
        return [
            cast(
                ChatCompletionMessageParam,
                {"role": "tool", "tool_call_id": r.tool_use_id, "content": r.content},
            )
            for r in message.tool_results
        ]
    if message.tool_uses:
        tool_calls = [
            {
                "id": u.id,
                "type": "function",
                "function": {"name": u.name, "arguments": json.dumps(u.input)},
            }
            for u in message.tool_uses
        ]
        return [
            cast(
                ChatCompletionMessageParam,
                {"role": "assistant", "content": message.text or None, "tool_calls": tool_calls},
            )
        ]
    role = "assistant" if message.role is Role.ASSISTANT else "user"
    return [cast(ChatCompletionMessageParam, {"role": role, "content": message.text})]


def _to_turn(response: ChatCompletion) -> Turn:
    choice = response.choices[0]
    message = choice.message
    tool_uses: list[ToolUse] = []
    for call in message.tool_calls or []:
        if isinstance(call, ChatCompletionMessageFunctionToolCall):
            tool_uses.append(
                ToolUse(
                    id=call.id,
                    name=call.function.name,
                    input=_parse_arguments(call.function.arguments),
                )
            )
    return Turn(
        text=message.content or "",
        tool_uses=tuple(tool_uses),
        stop_reason=_STOP_REASON.get(choice.finish_reason, choice.finish_reason),
    )


def _parse_arguments(raw: str) -> dict[str, object]:
    try:
        parsed: object = json.loads(raw)
    except json.JSONDecodeError:
        return {}
    if isinstance(parsed, dict):
        return {str(key): value for key, value in parsed.items()}
    return {}
