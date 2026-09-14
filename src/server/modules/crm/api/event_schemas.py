"""Schemas de los eventos."""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator

EventStatus = Literal["scheduled", "active", "closed"]

_NOMBRE_MAX = 120
_LOCATION_MAX = 300
_URL_MAX = 500


def _validate_maps_url(value: str | None) -> str | None:
    if value is None:
        return None
    cleaned = value.strip()
    if not cleaned:
        return None
    if not cleaned.startswith(("http://", "https://")):
        raise ValueError("El link de ubicación debe empezar con http:// o https://")
    return cleaned


class EventCreate(BaseModel):
    """Alta de un evento para un servicio."""

    service_id: uuid.UUID
    nombre: str = Field(min_length=1, max_length=_NOMBRE_MAX)
    starts_at: datetime
    location: str | None = Field(default=None, max_length=_LOCATION_MAX)
    maps_url: str | None = Field(default=None, max_length=_URL_MAX)
    # `None` = sin límite de cupo.
    capacity: int | None = Field(default=None, gt=0, le=100000)
    status: EventStatus = "scheduled"

    @field_validator("maps_url")
    @classmethod
    def _check_url(cls, value: str | None) -> str | None:
        return _validate_maps_url(value)


class EventUpdate(BaseModel):
    """Edición parcial: solo los campos presentes se aplican."""

    nombre: str | None = Field(default=None, min_length=1, max_length=_NOMBRE_MAX)
    starts_at: datetime | None = None
    location: str | None = Field(default=None, max_length=_LOCATION_MAX)
    maps_url: str | None = Field(default=None, max_length=_URL_MAX)
    capacity: int | None = Field(default=None, gt=0, le=100000)
    status: EventStatus | None = None

    @field_validator("maps_url")
    @classmethod
    def _check_url(cls, value: str | None) -> str | None:
        return _validate_maps_url(value)


class EventRead(BaseModel):
    """Evento + cuántas entradas lleva emitidas, para que el operador vea el cupo."""

    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    service_id: uuid.UUID
    nombre: str
    starts_at: datetime
    location: str | None
    maps_url: str | None
    capacity: int | None
    status: str
    issued: int = 0
    created_at: datetime
    updated_at: datetime
