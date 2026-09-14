"""Schemas de los links de entrega de un servicio (`service_link`).

`kind` viaja como `Literal` para validarse en el borde (422 automático). La URL se
valida como HTTP(S) porque termina dentro de un mensaje de WhatsApp: un valor roto
no se descubre acá, se descubre cuando el lead recibe un link que no abre.
"""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator

ServiceLinkKind = Literal["whatsapp_group", "meeting", "maps", "other"]

_LABEL_MAX = 60
_URL_MAX = 500


def _validate_url(value: str) -> str:
    cleaned = value.strip()
    if not cleaned.startswith(("http://", "https://")):
        raise ValueError("El link debe empezar con http:// o https://")
    if " " in cleaned:
        raise ValueError("El link no puede contener espacios")
    return cleaned


class ServiceLinkCreate(BaseModel):
    """Alta de link para un servicio."""

    kind: ServiceLinkKind
    url: str = Field(min_length=1, max_length=_URL_MAX)
    label: str | None = Field(default=None, max_length=_LABEL_MAX)
    orden: int = Field(default=0, ge=0)

    @field_validator("url")
    @classmethod
    def _check_url(cls, value: str) -> str:
        return _validate_url(value)


class ServiceLinkUpdate(BaseModel):
    """Edición parcial: solo los campos presentes se aplican."""

    kind: ServiceLinkKind | None = None
    url: str | None = Field(default=None, min_length=1, max_length=_URL_MAX)
    label: str | None = Field(default=None, max_length=_LABEL_MAX)
    orden: int | None = Field(default=None, ge=0)

    @field_validator("url")
    @classmethod
    def _check_url(cls, value: str | None) -> str | None:
        return None if value is None else _validate_url(value)


class ServiceLinkRead(BaseModel):
    """Link de entrega tal como lo ve el operador."""

    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    service_id: uuid.UUID
    kind: str
    url: str
    label: str | None
    orden: int
    created_at: datetime
    updated_at: datetime
