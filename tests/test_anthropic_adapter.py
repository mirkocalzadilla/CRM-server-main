"""Tests del AnthropicAdapter (server#288): temperatura según capacidad del modelo
(SDK v1 sin el kwarg → `extra_body`), auto-degradación ante un 400 por temperature y
traducción de excepciones del SDK a `LLMError`. Cliente falso: sin red."""

from __future__ import annotations

from typing import Any

import anthropic
import httpx2
import pytest
from anthropic.types import Message as AnthropicMessage
from anthropic.types import TextBlock, Usage

from server.modules.agent.domain.llm_port import LLMError, Role
from server.modules.agent.domain.llm_port import Message as PortMessage
from server.modules.agent.services.anthropic_adapter import AnthropicAdapter

_MODEL_WITH_TEMP = "claude-sonnet-4-6"
_MODEL_WITHOUT_TEMP = "claude-sonnet-5"  # 4.7+/5.x: el API no acepta temperature


def _response(text: str = "hola") -> AnthropicMessage:
    return AnthropicMessage(
        id="msg_test",
        content=[TextBlock(type="text", text=text)],
        model=_MODEL_WITH_TEMP,
        role="assistant",
        stop_reason="end_turn",
        stop_sequence=None,
        type="message",
        usage=Usage(input_tokens=1, output_tokens=1),
    )


def _bad_request(message: str) -> anthropic.BadRequestError:
    request = httpx2.Request("POST", "https://api.anthropic.com/v1/messages")
    response = httpx2.Response(400, request=request)
    return anthropic.BadRequestError(message, response=response, body=None)


def _status_error(status: int, cls: type[anthropic.APIStatusError]) -> anthropic.APIStatusError:
    request = httpx2.Request("POST", "https://api.anthropic.com/v1/messages")
    response = httpx2.Response(status, request=request)
    return cls("boom", response=response, body=None)


class _FakeMessages:
    """Devuelve (o levanta) los pasos en orden y registra los kwargs de cada create."""

    def __init__(self, steps: list[object]) -> None:
        self.calls: list[dict[str, Any]] = []
        self._steps = steps

    async def create(self, **kwargs: Any) -> AnthropicMessage:
        self.calls.append(kwargs)
        step = self._steps.pop(0)
        if isinstance(step, Exception):
            raise step
        assert isinstance(step, AnthropicMessage)
        return step


def _adapter(model: str, steps: list[object]) -> tuple[AnthropicAdapter, _FakeMessages]:
    adapter = AnthropicAdapter(api_key="test-key", model=model, max_tokens=64)
    fake = _FakeMessages(steps)
    adapter._client.messages = fake  # type: ignore[assignment]
    return adapter, fake


def _messages() -> list[PortMessage]:
    return [PortMessage(role=Role.USER, text="cuanto cuesta?")]


async def test_temperature_travels_via_extra_body_when_model_supports_it() -> None:
    adapter, fake = _adapter(_MODEL_WITH_TEMP, [_response()])
    turn = await adapter.complete(system="s", messages=_messages(), tools=[], temperature=0.3)
    assert turn.text == "hola"
    assert fake.calls[0]["extra_body"] == {"temperature": 0.3}
    assert "temperature" not in fake.calls[0]  # el kwarg no existe en el SDK v1


async def test_no_temperature_sends_no_extra_body() -> None:
    adapter, fake = _adapter(_MODEL_WITH_TEMP, [_response()])
    await adapter.complete(system="s", messages=_messages(), tools=[])
    assert fake.calls[0]["extra_body"] is None


async def test_temperature_omitted_for_models_that_reject_it() -> None:
    # El modelo (4.7+/5.x) no acepta temperature → se omite aunque el agente la tenga
    # configurada; al volver a un modelo que la acepta, vuelve a viajar.
    adapter, fake = _adapter(_MODEL_WITHOUT_TEMP, [_response()])
    await adapter.complete(system="s", messages=_messages(), tools=[], temperature=0.7)
    assert fake.calls[0]["extra_body"] is None


async def test_temperature_400_retries_without_and_disables() -> None:
    # Detección en runtime (server#288): si el API rechaza temperature pese al catálogo,
    # se reintenta una vez sin ella y no se vuelve a mandar en el proceso.
    adapter, fake = _adapter(
        _MODEL_WITH_TEMP,
        [_bad_request("temperature: Extra inputs are not permitted"), _response(), _response()],
    )
    turn = await adapter.complete(system="s", messages=_messages(), tools=[], temperature=0.3)
    assert turn.text == "hola"
    assert fake.calls[0]["extra_body"] == {"temperature": 0.3}
    assert fake.calls[1]["extra_body"] is None  # retry sin temperature
    await adapter.complete(system="s", messages=_messages(), tools=[], temperature=0.3)
    assert fake.calls[2]["extra_body"] is None  # quedó deshabilitada


async def test_unrelated_400_does_not_retry() -> None:
    adapter, fake = _adapter(_MODEL_WITH_TEMP, [_bad_request("max_tokens: too large")])
    with pytest.raises(LLMError) as excinfo:
        await adapter.complete(system="s", messages=_messages(), tools=[], temperature=0.3)
    assert excinfo.value.category == LLMError.BAD_REQUEST
    assert len(fake.calls) == 1  # sin retry: el 400 no era por temperature


@pytest.mark.parametrize(
    ("error", "category"),
    [
        (_status_error(401, anthropic.AuthenticationError), LLMError.AUTH),
        (_status_error(403, anthropic.PermissionDeniedError), LLMError.AUTH),
        (_status_error(429, anthropic.RateLimitError), LLMError.RATE_LIMIT),
        (_status_error(500, anthropic.InternalServerError), LLMError.PROVIDER),
        (
            anthropic.APIConnectionError(
                request=httpx2.Request("POST", "https://api.anthropic.com/v1/messages")
            ),
            LLMError.NETWORK,
        ),
        (TypeError("unexpected keyword argument"), LLMError.INTERNAL),
    ],
)
async def test_sdk_errors_translate_to_llm_error(error: Exception, category: str) -> None:
    adapter, _ = _adapter(_MODEL_WITH_TEMP, [error])
    with pytest.raises(LLMError) as excinfo:
        await adapter.complete(system="s", messages=_messages(), tools=[])
    assert excinfo.value.provider == "anthropic"
    assert excinfo.value.category == category
    assert isinstance(excinfo.value.__cause__, type(error))
