"""Schemas del catálogo (SPEC_admin_catalogo_kb §5): CRUD de servicios + categorías,
lectura de assets y resultado de `publish`.

Las enums de negocio (moneda/flujo_cierre/kind) viajan como `Literal` para validarse
en el borde (422 automático). La categoría es dinámica (tabla `service_category`,
#106): el servicio referencia `category_id` (puede ser null = sin categoría).
`ServiceUpdate` es parcial: solo los campos provistos se aplican. El `slug` no se
edita (identidad del servicio).
"""

from __future__ import annotations

import re
import uuid
from datetime import datetime
from decimal import Decimal, InvalidOperation
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator

Moneda = Literal["BOB", "USD"]
FlujoCierre = Literal["pago_qr", "handoff_consultivo"]
AssetKind = Literal["pdf", "image"]
# Post-payment delivery modality. Absent/`null` = the service delivers nothing on
# payment: safe default, covers everything that is not a course. `hibrido` is in person
# and virtual at once (QR entry plus the meeting links), which is what Mirko's 4-module
# course actually is.
Modalidad = Literal["presencial", "virtual", "hibrido"]

_SLUG_PATTERN = r"^[a-z0-9]+(?:-[a-z0-9]+)*$"
# Field length limits (#107): name 50, slug 40, summary 200, detail 300.
_NOMBRE_MAX = 50
_SLUG_MAX = 40
_RESUMEN_MAX = 200
_DETALLE_MAX = 300
# Price: only digits with up to 2 decimals, max 100000.00 (#107).
_PRECIO_PATTERN = re.compile(r"^\d+(\.\d{1,2})?$")
_PRECIO_MAX = Decimal("100000.00")
# Materials: at most 5 files per service (#108).
MAX_MATERIALS = 5


def _validate_precio(value: str) -> str:
    """Precio numérico: solo dígitos, hasta 2 decimales, máx. 100000.00."""
    cleaned = value.strip()
    if not _PRECIO_PATTERN.match(cleaned):
        raise ValueError("Precio inválido: solo números con hasta 2 decimales.")
    try:
        amount = Decimal(cleaned)
    except InvalidOperation as exc:  # pragma: no cover - regex ya lo garantiza
        raise ValueError("Precio inválido.") from exc
    if amount > _PRECIO_MAX:
        raise ValueError("Precio máximo permitido: 100000.00.")
    return cleaned


class AssetRead(BaseModel):
    """Material subido visible para el operador / preview."""

    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    kind: AssetKind
    filename: str
    public_url: str
    bytes: int
    created_at: datetime


class ServiceCategoryCreate(BaseModel):
    """Alta de categoría del catálogo (ABM por organización). El `slug` se deriva del
    nombre en el server. `asset_ids` = materiales (PDF/imagen) de la categoría (#235)."""

    nombre: str = Field(min_length=1, max_length=120)
    orden: int = Field(default=0, ge=0)
    asset_ids: list[uuid.UUID] = Field(default_factory=list, max_length=MAX_MATERIALS)


class ServiceCategoryUpdate(BaseModel):
    """Edición parcial de categoría. `asset_ids` ausente = no tocar los materiales;
    `[]` = quitarlos todos."""

    nombre: str | None = Field(default=None, min_length=1, max_length=120)
    orden: int | None = Field(default=None, ge=0)
    asset_ids: list[uuid.UUID] | None = Field(default=None, max_length=MAX_MATERIALS)


class ServiceCategoryRead(BaseModel):
    """Categoría del catálogo + su slug y sus materiales."""

    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    organization_id: uuid.UUID
    nombre: str
    slug: str
    orden: int
    materials: list[AssetRead]
    created_at: datetime
    updated_at: datetime


class ServiceCreate(BaseModel):
    """Alta de servicio. `slug` único por agente; `category_id` opcional (dinámica)."""

    slug: str = Field(pattern=_SLUG_PATTERN, max_length=_SLUG_MAX)
    nombre: str = Field(min_length=1, max_length=_NOMBRE_MAX)
    category_id: uuid.UUID | None = None
    resumen: str = Field(min_length=1, max_length=_RESUMEN_MAX)
    detalle: str | None = Field(default=None, max_length=_DETALLE_MAX)
    precio: str = Field(min_length=1, max_length=120)
    moneda: Moneda
    flujo_cierre: FlujoCierre = "pago_qr"
    orden: int = Field(default=0, ge=0)
    modality: Modalidad | None = None
    price_amount: Decimal | None = Field(default=None, gt=0, le=_PRECIO_MAX, decimal_places=2)

    @field_validator("precio")
    @classmethod
    def _check_precio(cls, value: str) -> str:
        return _validate_precio(value)


class ServiceUpdate(BaseModel):
    """Edición parcial: solo los campos presentes se aplican. `slug` inmutable."""

    nombre: str | None = Field(default=None, min_length=1, max_length=_NOMBRE_MAX)
    category_id: uuid.UUID | None = None
    resumen: str | None = Field(default=None, min_length=1, max_length=_RESUMEN_MAX)
    detalle: str | None = Field(default=None, max_length=_DETALLE_MAX)
    precio: str | None = Field(default=None, min_length=1, max_length=120)
    moneda: Moneda | None = None
    flujo_cierre: FlujoCierre | None = None
    orden: int | None = Field(default=None, ge=0)
    is_active: bool | None = None
    # Ausentes = no tocar; `null` explícito = borrar el valor (vuelve al default
    # seguro: sin modalidad no hay entrega, sin monto no hay auto-validación).
    modality: Modalidad | None = None
    price_amount: Decimal | None = Field(default=None, gt=0, le=_PRECIO_MAX, decimal_places=2)

    @field_validator("precio")
    @classmethod
    def _check_precio(cls, value: str | None) -> str | None:
        return None if value is None else _validate_precio(value)


class ServiceRead(BaseModel):
    """Servicio del catálogo + su categoría (el material vive en la categoría, #235)."""

    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    organization_id: uuid.UUID
    agent_id: uuid.UUID
    slug: str
    nombre: str
    category_id: uuid.UUID | None
    category: ServiceCategoryRead | None
    resumen: str
    detalle: str | None
    precio: str
    moneda: str
    flujo_cierre: str
    orden: int
    is_active: bool
    modality: str | None
    price_amount: Decimal | None
    created_at: datetime
    updated_at: datetime
