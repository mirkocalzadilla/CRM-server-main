"""Schemas de la API CRM (slice 2). Respuestas read-model + requests de operación."""

from __future__ import annotations

import uuid
from datetime import datetime

from pydantic import BaseModel, Field, field_validator


class CardOut(BaseModel):
    id: uuid.UUID
    title: str
    conversation_id: uuid.UUID
    stage_id: uuid.UUID
    phone: str  # external_id (wa_id) de la conversación; habilita la búsqueda por número
    rating: str  # 'hot' | 'medium' | 'cold' — calificación del lead (badge en la card)
    alert: str | None = None  # etiqueta de alerta derivada del motivo de handoff (#94)
    is_ai_active: bool = True  # takeover: la IA responde (True) o atiende un humano (False)
    # True cuando la IA está apagada y el último mensaje del hilo es del lead sin
    # respuesta humana posterior: hay alguien esperando y nadie contestó (#175/UX).
    awaiting_human: bool = False
    # Avisos operativos: por qué algo automático no se completó (falta el nombre del
    # lead, se cerró la ventana de 24h, llegó un comprobante extra…). Códigos; el CRM
    # los traduce. Ver `crm/domain/card_flags.py`.
    flags: list[str] = []
    # Lo último que pasó en la conversación (mensaje del lead o respuesta). `None` si no
    # hubo ninguno: alta manual de oportunidad sin chat. Ordena la cola de atención por
    # cuánto lleva esperando, que es el dato útil — no por cuándo se creó la card.
    last_activity_at: datetime | None = None
    # Cuándo una persona la marcó como atendida (0036); `None` si nadie lo hizo.
    attended_at: datetime | None = None
    created_at: datetime


class StageOut(BaseModel):
    id: uuid.UUID
    name: str
    position: int
    status_code: str
    cards: list[CardOut]


class PipelineOut(BaseModel):
    id: uuid.UUID
    kind: str
    name: str
    position: int
    stages: list[StageOut]


class BoardOut(BaseModel):
    pipelines: list[PipelineOut]


class ThreadMessage(BaseModel):
    sender: str  # 'lead' | 'agent' | 'human' | 'system' (evento de error, server#288)
    text: str
    at: datetime
    type: str = "text"  # 'text' | 'image' | 'document' | 'error'
    media_url: str | None = None
    # Código de la falla del agente cuando type='error' (LLMError.* | 'internal' |
    # 'delivery'); el CRM lo traduce a copy.
    category: str | None = None


class CardMoveOut(BaseModel):
    """Un paso del historial de movimientos de la card (traceability)."""

    stage_from_name: str | None  # None en el alta de la card (primer move sin origen)
    stage_from_color: str | None
    stage_to_name: str
    stage_to_color: str | None
    moved_by: str  # 'agent' | user_id::text
    reason: str | None  # motivo del move manual (#253); None en filas viejas y syncs del bot
    moved_at: datetime


class CardContactOut(BaseModel):
    """Contacto vinculado a la card, resuelto vía la FK `card.contact_id` (#139)."""

    id: uuid.UUID
    full_name: str | None


class CardServiceOut(BaseModel):
    """Servicio del catálogo asignado a la card (#132), resuelto desde el catálogo vivo.
    `source`: 'assigned' (operador) | 'captured' (el bot lo eligió, #133)."""

    id: uuid.UUID  # id del card_service (para quitarlo)
    service_id: uuid.UUID
    nombre: str
    precio: str
    moneda: str
    source: str


class CardDetailOut(BaseModel):
    id: uuid.UUID
    title: str
    conversation_id: uuid.UUID
    stage_id: uuid.UUID
    is_ai_active: bool
    phone: str  # external_id (wa_id) de la conversación
    full_name: str | None  # conversation.full_name; prefill del nombre en el form de edición
    notes: str | None  # notas libres de la oportunidad
    rating: str  # 'hot' | 'medium' | 'cold' — calificación del lead
    alert: str | None = None  # etiqueta de alerta derivada del motivo de handoff (#94)
    ai_summary: str | None  # resumen del caso por IA (conversation.ai_summary)
    thread: list[ThreadMessage]
    moves: list[CardMoveOut]  # historial cronológico (asc por moved_at)
    contact: CardContactOut | None  # contacto vinculado (FK) o None si el número no es contacto
    services: list[CardServiceOut]  # servicios del catálogo asignados/capturados (#132)
    flags: list[str] = []  # avisos operativos de la entrega (ver `crm/domain/card_flags.py`)
    created_at: datetime


class CardCreate(BaseModel):
    """Alta manual de oportunidad: crea conversación (chat vacío) + card en el primer
    stage. Idempotente por teléfono."""

    phone: str = Field(..., min_length=1, max_length=32)
    full_name: str | None = Field(default=None, max_length=255)
    notes: str | None = Field(default=None, max_length=2000)


class CardUpdate(BaseModel):
    """Edición de detalles. Campos ausentes no se tocan (exclude_unset). `full_name`
    actualiza `conversation.full_name` + `card.title`."""

    full_name: str | None = Field(default=None, max_length=255)
    notes: str | None = Field(default=None, max_length=2000)


class QrEntryOut(BaseModel):
    card_id: uuid.UUID
    token: str
    qr_ref: str


class ContactOut(BaseModel):
    id: uuid.UUID
    phone: str  # espeja conversation.external_id (wa_id)
    full_name: str | None
    created_at: datetime


class ContactCreate(BaseModel):
    """ABM manual: el nombre es obligatorio — no puede haber un contacto sin nombre.
    El alta automática del hook 'won' no pasa por acá (usa el upsert del repo)."""

    phone: str = Field(..., min_length=1, max_length=32)
    full_name: str = Field(..., min_length=1, max_length=255)

    @field_validator("full_name")
    @classmethod
    def _full_name_not_blank(cls, value: str) -> str:
        value = value.strip()
        if not value:
            raise ValueError("el nombre del contacto es obligatorio")
        return value


class ContactUpdate(BaseModel):
    full_name: str = Field(..., min_length=1, max_length=255)

    @field_validator("full_name")
    @classmethod
    def _full_name_not_blank(cls, value: str) -> str:
        value = value.strip()
        if not value:
            raise ValueError("el nombre del contacto es obligatorio")
        return value


class CardServicesIn(BaseModel):
    """Set completo de servicios asignados manualmente a la card (#132). Reemplaza los
    `assigned` actuales; los `captured` por el bot no se tocan."""

    service_ids: list[uuid.UUID] = Field(default_factory=list, max_length=20)


class MoveRequest(BaseModel):
    stage_id: uuid.UUID
    # Motivo opcional del move (audit, #253). Obligatorio para Descalificado, pero eso
    # se impone en la UI: la API lo acepta siempre opcional.
    reason: str | None = Field(default=None, max_length=2000)


class AiActiveRequest(BaseModel):
    is_ai_active: bool


class SendRequest(BaseModel):
    text: str = Field(..., min_length=1, max_length=4096)


class SendResponse(BaseModel):
    sender: str
    text: str
    at: datetime
    type: str = "text"  # 'text' | 'image' | 'document' (adjuntos del takeover, #251)
    media_url: str | None = None
