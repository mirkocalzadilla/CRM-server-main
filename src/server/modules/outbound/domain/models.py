"""Outbound (business-initiated) WhatsApp messages: template sends + opt-outs.

Every template send is a row here, before and after Meta answers. Meta's delivery
statuses (webhook `statuses`) update `status` by `wamid`; without this table a
message Meta silently drops after a 2xx is invisible (#298).
"""

from __future__ import annotations

import uuid
from datetime import datetime

from sqlalchemy import DateTime, Integer, Text, UniqueConstraint, func
from sqlalchemy.dialects.postgresql import JSON, UUID
from sqlalchemy.orm import Mapped, mapped_column

from server.shared.base_model import Base, TimestampMixin, UUIDPrimaryKeyMixin

STATUS_QUEUED = "queued"
STATUS_SENT = "sent"
STATUS_DELIVERED = "delivered"
STATUS_READ = "read"
STATUS_FAILED = "failed"
STATUS_SKIPPED = "skipped"  # not sent on purpose: opt-out, duplicate, window

# Why a template went out. Drives dedupe keys and daily caps.
PURPOSE_ENTRY = "entry"
PURPOSE_EVENT_REMINDER = "event_reminder"
PURPOSE_REACTIVATION = "reactivation"
PURPOSE_MANUAL = "manual"

# Meta status precedence: a late `sent` must not override `delivered`/`read`.
_STATUS_RANK = {
    STATUS_QUEUED: 0,
    STATUS_SENT: 1,
    STATUS_DELIVERED: 2,
    STATUS_READ: 3,
    STATUS_FAILED: 4,
    STATUS_SKIPPED: 4,
}


def status_rank(status: str) -> int:
    return _STATUS_RANK.get(status, 0)


class OutboundMessage(Base, UUIDPrimaryKeyMixin, TimestampMixin):
    __tablename__ = "outbound_message"
    __table_args__ = (UniqueConstraint("dedupe_key", name="uq_outbound_dedupe_key"),)

    organization_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), nullable=False, index=True
    )
    conversation_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), index=True)
    card_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), index=True)
    wa_id: Mapped[str] = mapped_column(Text, nullable=False, index=True)
    template_name: Mapped[str] = mapped_column(Text, nullable=False)
    language: Mapped[str] = mapped_column(Text, nullable=False, default="es")
    purpose: Mapped[str] = mapped_column(Text, nullable=False, index=True)
    variables: Mapped[dict[str, object]] = mapped_column(JSON, nullable=False, default=dict)
    rendered_text: Mapped[str] = mapped_column(Text, nullable=False, default="")
    dedupe_key: Mapped[str | None] = mapped_column(Text)
    wamid: Mapped[str | None] = mapped_column(Text, unique=True)
    status: Mapped[str] = mapped_column(Text, nullable=False, default=STATUS_QUEUED, index=True)
    error_code: Mapped[int | None] = mapped_column(Integer)
    error_detail: Mapped[str | None] = mapped_column(Text)
    sent_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    status_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class MarketingOptOut(Base, UUIDPrimaryKeyMixin):
    """A phone that asked not to receive business-initiated messages (BAJA)."""

    __tablename__ = "marketing_opt_out"
    __table_args__ = (UniqueConstraint("organization_id", "wa_id", name="uq_opt_out_org_wa"),)

    organization_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    wa_id: Mapped[str] = mapped_column(Text, nullable=False)
    source: Mapped[str] = mapped_column(Text, nullable=False, default="keyword")
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )


# Keep the per-organization settings table in the same metadata import path.
from server.modules.outbound.domain.settings_models import OutboundSettings  # noqa: E402,F401
