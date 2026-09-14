"""Provider selection for the LLM runtime (§6).

`build_llm` is the single place that decides which `LLMPort` implementation backs
a given role. `LLM_PROVIDER` picks the provider (Anthropic is the default); `role`
picks the model — `loop` is the live conversation with the lead (quality tier,
Sonnet), `summary` is any batch summarization (cheap tier, Haiku). Prompt caching
in the adapter applies to both, so the Sonnet loop keeps the cached prefix.

`build_vision` es el equivalente para el puerto de visión (leer un comprobante). Va
aparte porque el contrato es otro (`VisionPort`, un binario y un schema) y porque el
modelo también: la extracción es una tarea acotada y se resuelve en el tier barato.
"""

from __future__ import annotations

from typing import Literal

from server.config import Settings
from server.modules.agent.domain.llm_port import LLMPort
from server.modules.agent.domain.vision_port import VisionPort
from server.modules.agent.services.anthropic_adapter import AnthropicAdapter
from server.modules.agent.services.anthropic_vision import AnthropicVisionAdapter
from server.modules.agent.services.openai_adapter import OpenAIAdapter
from server.modules.agent.services.openai_vision import OpenAIVisionAdapter

LLMRole = Literal["loop", "summary"]


def build_llm(settings: Settings, *, role: LLMRole) -> LLMPort:
    if settings.llm_provider == "openai":
        model = (
            settings.llm_openai_model_loop if role == "loop" else settings.llm_openai_model_summary
        )
        return OpenAIAdapter(
            api_key=settings.openai_api_key,
            model=model,
            max_tokens=settings.llm_max_tokens,
        )
    # loop = live conversation → Sonnet (best UX); summary = batch → Haiku (cheap).
    model = settings.llm_model_sonnet if role == "loop" else settings.llm_model_haiku
    return AnthropicAdapter(
        api_key=settings.anthropic_api_key,
        model=model,
        max_tokens=settings.llm_max_tokens,
    )


def build_vision(settings: Settings) -> VisionPort:
    """Adapter de visión del provider activo. Extraer campos de un comprobante es una
    tarea acotada: no necesita el tier de calidad que usa la conversación."""
    if settings.llm_provider == "openai":
        return OpenAIVisionAdapter(
            api_key=settings.openai_api_key,
            model=settings.llm_openai_model_vision,
            max_tokens=settings.llm_vision_max_tokens,
        )
    return AnthropicVisionAdapter(
        api_key=settings.anthropic_api_key,
        model=settings.llm_model_vision,
        max_tokens=settings.llm_vision_max_tokens,
    )
