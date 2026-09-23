"""Per-organization configuration of the automated follow-ups (etapa D)."""

from __future__ import annotations

import uuid
from dataclasses import dataclass

from sqlalchemy import Boolean, Integer, Text
from sqlalchemy.dialects.postgresql import JSON, UUID
from sqlalchemy.orm import Mapped, mapped_column

from server.shared.base_model import Base, TimestampMixin, UUIDPrimaryKeyMixin

DEFAULT_DAILY_CAP = 100
DEFAULT_RECONTACT_DAYS = 30
# Agreed with the business on 2026-09-23: warm leads after a week, cold ones after two.
DEFAULT_RULES: list[dict[str, object]] = [
    {"stages": ["engaging", "qualified"], "days": 7},
    {"stages": ["new"], "days": 14},
]


@dataclass(frozen=True)
class ReactivationRule:
    stages: tuple[str, ...]
    inactive_days: int


def parse_rules(raw: object) -> list[ReactivationRule]:
    """Tolerant parse of the JSON rules; malformed entries are ignored."""
    rules: list[ReactivationRule] = []
    if not isinstance(raw, list):
        return rules
    for item in raw:
        if not isinstance(item, dict):
            continue
        stages = item.get("stages")
        days = item.get("days")
        if isinstance(stages, list) and stages and isinstance(days, int) and days > 0:
            rules.append(ReactivationRule(tuple(str(s) for s in stages), days))
    return rules


class OutboundSettings(Base, UUIDPrimaryKeyMixin, TimestampMixin):
    __tablename__ = "outbound_settings"

    organization_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), nullable=False, unique=True
    )
    reactivation_enabled: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    reactivation_daily_cap: Mapped[int] = mapped_column(
        Integer, nullable=False, default=DEFAULT_DAILY_CAP
    )
    reactivation_recontact_days: Mapped[int] = mapped_column(
        Integer, nullable=False, default=DEFAULT_RECONTACT_DAYS
    )
    reactivation_rules: Mapped[list[dict[str, object]]] = mapped_column(
        JSON, nullable=False, default=lambda: list(DEFAULT_RULES)
    )
    # "{{3}}" of `reactivacion_leads`: what is new this month, written by the business.
    novelty_text: Mapped[str] = mapped_column(Text, nullable=False, default="")

    @property
    def rules(self) -> list[ReactivationRule]:
        return parse_rules(self.reactivation_rules)
