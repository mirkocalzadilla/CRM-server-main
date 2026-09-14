"""Schemas de M-Config: lectura/edición del agente versionada (SPEC_M-Config §3.A).

`PUT /agents/{id}` recibe `AgentUpdate` (campos opcionales; al menos uno) y
devuelve la `AgentVersion` recién creada. `temperature` viaja dentro de `config`
y se valida acá (0..1: rango seguro para ambos providers). `emojis` también viaja
en `config` y es un switch booleano on/off (#104; aplicación al prompt = Ola 3).
"""

from __future__ import annotations

import uuid
from datetime import datetime

from pydantic import BaseModel, ConfigDict, Field, model_validator


class ModelPricingRead(BaseModel):
    """Precio informativo en USD por millón de tokens."""

    model_config = ConfigDict(from_attributes=True)

    input: float
    cached_input: float
    output: float


class ModelRead(BaseModel):
    """Una entrada del catálogo de modelos permitidos (pobla el dropdown FE #60)."""

    model_config = ConfigDict(from_attributes=True)

    id: str
    label: str
    provider: str
    reasoning: bool
    reasoning_effort: str | None
    pricing: ModelPricingRead | None
    # False cuando el API del modelo no acepta `temperature` (Anthropic 4.7+/5.x,
    # reasoning de OpenAI): el front deshabilita/anota el slider (server#288).
    supports_temperature: bool


class AgentVersionRead(BaseModel):
    """Snapshot versionado completo (fila de `agent_version`)."""

    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    agent_id: uuid.UUID
    version_number: int
    system_prompt: str
    model: str
    tools: list[object]
    config: dict[str, object]
    change_summary: str | None
    created_by: uuid.UUID | None
    created_at: datetime


class AgentRead(BaseModel):
    """Config viva del agente + su versión activa (si ya fue editado)."""

    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    organization_id: uuid.UUID
    display_name: str
    system_prompt: str
    model: str
    tools: list[object]
    config: dict[str, object]
    is_active: bool
    current_version: AgentVersionRead | None = None


class AgentUpdate(BaseModel):
    """Cambios a aplicar; `config` reemplaza el JSON (salvo dos reglas server-side,
    #105): los nodos manuales `ofertas`/`faq`/`resumen` se descartan (vienen del
    Catálogo de Servicios o quedan deprecados) y la key `services` no se pisa (es
    server-owned, la escribe `CatalogService.publish`). Ver `AgentConfigService`."""

    display_name: str | None = Field(default=None, min_length=1, max_length=120)
    system_prompt: str | None = Field(default=None, min_length=1)
    model: str | None = Field(default=None, min_length=1)
    config: dict[str, object] | None = None
    change_summary: str | None = Field(default=None, max_length=500)

    @model_validator(mode="after")
    def _validate(self) -> AgentUpdate:
        if (
            self.display_name is None
            and self.system_prompt is None
            and self.model is None
            and self.config is None
        ):
            raise ValueError(
                "Se requiere al menos un cambio (display_name, system_prompt, model o config)"
            )
        if self.config is not None and "temperature" in self.config:
            value = self.config["temperature"]
            if isinstance(value, bool) or not isinstance(value, int | float):
                raise ValueError("config.temperature debe ser numérico")
            if not 0.0 <= float(value) <= 1.0:
                raise ValueError("config.temperature debe estar entre 0.0 y 1.0")
        # emojis: switch on/off (#104). El runtime que instruye usar/no emojis es Ola 3.
        if (
            self.config is not None
            and "emojis" in self.config
            and not isinstance(self.config["emojis"], bool)
        ):
            raise ValueError("config.emojis debe ser booleano (switch on/off)")
        return self
