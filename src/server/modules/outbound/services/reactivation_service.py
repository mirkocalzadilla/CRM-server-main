"""Etapa D — `reactivacion_leads` to cold leads, by configurable rules.

Off by default per organization. When enabled, each tick (inside send hours) walks
the rules (`stages` + `days` without a lead message), skips leads contacted recently,
opted out, closed or taken over by a human, and sends up to the daily cap. Marketing
templates cost money and bans hurt the number, so every guard errs on not sending.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

from sqlalchemy.ext.asyncio import AsyncSession

from server.config import get_settings
from server.modules.agent.repositories.ai_chat_history_repository import AiChatHistoryRepository
from server.modules.outbound.domain.models import PURPOSE_REACTIVATION
from server.modules.outbound.domain.schedule import as_utc, is_within_send_hours
from server.modules.outbound.domain.settings_models import OutboundSettings
from server.modules.outbound.domain.templates import REACTIVACION_LEADS
from server.modules.outbound.repositories.outbound_repository import OutboundMessageRepository
from server.modules.outbound.repositories.reactivation_repository import (
    Candidate,
    ReactivationRepository,
)
from server.modules.outbound.services.entry_delivery import first_name
from server.modules.outbound.services.template_sender import (
    SendRequest,
    TemplateSender,
    TemplateSenderPort,
)
from server.shared.logger import get_logger
from server.shared.timezone import to_business_time

logger = get_logger(__name__)

_GENERIC_INTEREST = "nuestros talleres y servicios"


@dataclass(frozen=True)
class ReactivationRun:
    sent: int = 0
    skipped: int = 0
    reason: str | None = None


def _local_day_start(now: datetime) -> datetime:
    local = to_business_time(now)
    return local.replace(hour=0, minute=0, second=0, microsecond=0).astimezone(UTC)


class ReactivationService:
    def __init__(self, session: AsyncSession, sender: TemplateSenderPort) -> None:
        self._repo = ReactivationRepository(session)
        self._outbound = OutboundMessageRepository(session)
        self._history = AiChatHistoryRepository(session)
        self._templates = TemplateSender(session, sender)
        self._settings = get_settings()

    async def run(self, now: datetime | None = None) -> ReactivationRun:
        now = now or datetime.now(UTC)
        if not is_within_send_hours(
            now, self._settings.outbound_send_hour_start, self._settings.outbound_send_hour_end
        ):
            return ReactivationRun(reason="outside_send_hours")
        total = ReactivationRun()
        for org_settings in await self._repo.enabled_settings():
            run = await self.run_for(org_settings, now)
            total = ReactivationRun(total.sent + run.sent, total.skipped + run.skipped)
        return total

    async def run_for(self, cfg: OutboundSettings, now: datetime) -> ReactivationRun:
        if not cfg.novelty_text.strip():
            return ReactivationRun(reason="no_novelty_text")  # "{{3}}" would be empty
        org_id = cfg.organization_id
        already = await self._outbound.count_sent_since(
            org_id, PURPOSE_REACTIVATION, _local_day_start(now)
        )
        budget = max(cfg.reactivation_daily_cap - already, 0)
        sent = skipped = 0
        seen: set[str] = set()
        for rule in cfg.rules:
            if budget <= 0:
                break
            for candidate in await self._repo.candidates(org_id, rule.stages):
                if budget <= 0:
                    break
                conv = candidate.conversation
                if conv.id.hex in seen:
                    continue
                seen.add(conv.id.hex)
                if not await self._eligible(candidate, cfg, now, rule.inactive_days):
                    skipped += 1
                    continue
                if await self.send_to(candidate, cfg, now):
                    sent += 1
                    budget -= 1
                else:
                    skipped += 1
        if sent:
            logger.info("outbound.reactivation_sent", org=str(org_id), sent=sent, skipped=skipped)
        return ReactivationRun(sent=sent, skipped=skipped)

    async def _eligible(
        self, candidate: Candidate, cfg: OutboundSettings, now: datetime, inactive_days: int
    ) -> bool:
        conv = candidate.conversation
        activity = await self._history.last_activity_by_thread([str(conv.id)])
        last_user, _ = activity.get(str(conv.id), (None, None))
        reference = as_utc(last_user) if last_user is not None else as_utc(conv.created_at)
        if now - reference < timedelta(days=inactive_days):
            return False
        recent = await self._outbound.last_sent_to(
            conv.organization_id, conv.external_id, timedelta(days=cfg.reactivation_recontact_days)
        )
        return recent is None

    async def send_to(
        self, candidate: Candidate, cfg: OutboundSettings, now: datetime, *, manual: bool = False
    ) -> bool:
        """One reactivation to one lead. Manual sends get their own dedupe key."""
        conv = candidate.conversation
        interest = await self._repo.first_service_name(candidate.card_id) or _GENERIC_INTEREST
        result = await self._templates.send(
            SendRequest(
                organization_id=conv.organization_id,
                wa_id=conv.external_id,
                template=REACTIVACION_LEADS,
                variables=[first_name(conv.full_name), interest, cfg.novelty_text.strip()],
                purpose=PURPOSE_REACTIVATION,
                conversation_id=conv.id,
                card_id=candidate.card_id,
                agent_id=conv.instance.agent_id,
                dedupe_key=(
                    f"reactivation:{conv.id}:manual:{now.timestamp():.0f}"
                    if manual
                    else f"reactivation:{conv.id}:{to_business_time(now):%Y-%m-%d}"
                ),
            )
        )
        return result.sent
