"""Catálogo de modelos LLM permitidos para la config del agente (#103).

Fuente única de los modelos que el dropdown del front puede ofrecer (FE #60). Los
IDs van **pineados** (CLAUDE.md: nunca alias `-latest`/`-mini` suelto) y cada
entrada lleva su `provider` — esto deja listo el cableado futuro `agent.model →
llm_factory.build_llm` (runtime real per-agente = follow-up, ver #103 decisión B).
Hoy la selección persiste en `Agent.model`; el runtime sigue eligiendo por
`Settings`. `pricing` es informativo (USD por millón de tokens); `None` donde no se
tiene el dato real (no se inventan números).
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class ModelPricing:
    """Precio en USD por millón de tokens."""

    input: float
    cached_input: float
    output: float


@dataclass(frozen=True)
class ModelDescriptor:
    id: str
    label: str
    provider: str  # "anthropic" | "openai" — habilita el cableado futuro a build_llm
    reasoning: bool
    reasoning_effort: str | None
    pricing: ModelPricing | None
    # Whether the provider API accepts a sampling `temperature` for this model.
    # Anthropic removed it for 4.7+/5.x models; OpenAI reasoning models reject it.
    supports_temperature: bool = True


MODEL_CATALOG: tuple[ModelDescriptor, ...] = (
    ModelDescriptor(
        id="claude-haiku-4-5-20251001",
        label="Claude Haiku 4.5",
        provider="anthropic",
        reasoning=False,
        reasoning_effort=None,
        pricing=None,
    ),
    ModelDescriptor(
        id="claude-sonnet-4-6",
        label="Claude Sonnet 4.6",
        provider="anthropic",
        reasoning=False,
        reasoning_effort=None,
        pricing=None,
    ),
    ModelDescriptor(
        id="gpt-4o-mini-2024-07-18",
        label="GPT-4o mini",
        provider="openai",
        reasoning=False,
        reasoning_effort=None,
        pricing=None,
    ),
    ModelDescriptor(
        id="gpt-5.4-mini-2026-03-17",
        label="GPT-5.4 mini · reasoning=medium",
        provider="openai",
        reasoning=True,
        reasoning_effort="medium",
        pricing=ModelPricing(input=0.75, cached_input=0.08, output=4.5),
        supports_temperature=False,  # reasoning models reject sampling params
    ),
    ModelDescriptor(
        id="gpt-5.4-2026-03-05",
        label="GPT-5.4 · reasoning=high",
        provider="openai",
        reasoning=True,
        reasoning_effort="high",
        pricing=ModelPricing(input=2.5, cached_input=0.25, output=15.0),
        supports_temperature=False,  # reasoning models reject sampling params
    ),
)

# Families known to accept `temperature`, for runtime model IDs not in the catalog
# (`Settings.llm_model_*`). Anthropic dropped sampling params on 4.7+/5.x and the
# v1 SDK removed the kwarg, so an unknown/newer model defaults to NOT sending it —
# omitting is always safe (provider default), sending can reject the request.
_TEMPERATURE_MODEL_PREFIXES: tuple[str, ...] = (
    "claude-3",
    "claude-haiku-4-5",
    "claude-sonnet-4-5",
    "claude-sonnet-4-6",
    "claude-opus-4-5",
    "claude-opus-4-6",
    "gpt-4o",
)


def model_supports_temperature(model_id: str) -> bool:
    """True if the provider API accepts `temperature` for this model ID."""
    for descriptor in MODEL_CATALOG:
        if descriptor.id == model_id:
            return descriptor.supports_temperature
    return model_id.startswith(_TEMPERATURE_MODEL_PREFIXES)
