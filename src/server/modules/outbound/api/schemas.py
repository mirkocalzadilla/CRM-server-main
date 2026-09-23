from __future__ import annotations

import uuid
from datetime import datetime

from pydantic import BaseModel, ConfigDict, Field, field_validator

_NOVELTY_MAX = 300
_STAGES = {"new", "engaging", "qualifying", "qualified", "handed_off"}


class OutboundMessageRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    conversation_id: uuid.UUID | None
    card_id: uuid.UUID | None
    wa_id: str
    template_name: str
    purpose: str
    rendered_text: str
    status: str
    error_code: int | None
    error_detail: str | None
    sent_at: datetime | None
    status_at: datetime | None
    created_at: datetime


class OutboundMessagePage(BaseModel):
    items: list[OutboundMessageRead]
    limit: int
    offset: int


class ReactivationRuleIn(BaseModel):
    stages: list[str] = Field(min_length=1)
    days: int = Field(ge=1, le=365)

    @field_validator("stages")
    @classmethod
    def _known_stages(cls, value: list[str]) -> list[str]:
        unknown = sorted(set(value) - _STAGES)
        if unknown:
            raise ValueError(f"etapas desconocidas: {', '.join(unknown)}")
        return value


class OutboundSettingsRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    reactivation_enabled: bool
    reactivation_daily_cap: int
    reactivation_recontact_days: int
    reactivation_rules: list[dict[str, object]]
    novelty_text: str
    updated_at: datetime


class OutboundSettingsUpdate(BaseModel):
    """Partial update: only present fields apply."""

    reactivation_enabled: bool | None = None
    reactivation_daily_cap: int | None = Field(default=None, ge=1, le=2000)
    reactivation_recontact_days: int | None = Field(default=None, ge=1, le=365)
    reactivation_rules: list[ReactivationRuleIn] | None = None
    novelty_text: str | None = Field(default=None, max_length=_NOVELTY_MAX)


class OptOutRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    wa_id: str
    source: str
    created_at: datetime


class OptOutCreate(BaseModel):
    wa_id: str = Field(min_length=6, max_length=20, pattern=r"^\d+$")


class ManualSendResult(BaseModel):
    sent: bool
    reason: str | None = None
