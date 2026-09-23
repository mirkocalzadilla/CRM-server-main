"""Etapa C — `recordatorio_evento` to every live entry, N hours before the event.

Runs from the worker on a timer. Idempotent through `dedupe_key`
(`reminder:{event}:{entry}:{hours}h`), so ticks can overlap or repeat safely. Only the
closest due window is sent per tick (see `due_reminder_window`), and nothing goes
out outside the business send hours.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

from sqlalchemy.ext.asyncio import AsyncSession

from server.config import get_settings
from server.modules.crm.domain.event_models import Event
from server.modules.outbound.domain.models import PURPOSE_EVENT_REMINDER
from server.modules.outbound.domain.schedule import due_reminder_window, is_within_send_hours
from server.modules.outbound.domain.templates import RECORDATORIO_EVENTO
from server.modules.outbound.repositories.reminder_repository import (
    ReminderRepository,
    ReminderTarget,
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

_NO_LOCATION = "el lugar indicado en tu entrada"


@dataclass(frozen=True)
class ReminderRun:
    sent: int = 0
    skipped: int = 0
    reason: str | None = None


class EventReminderService:
    def __init__(self, session: AsyncSession, sender: TemplateSenderPort) -> None:
        self._repo = ReminderRepository(session)
        self._templates = TemplateSender(session, sender)
        self._settings = get_settings()

    async def run(self, now: datetime | None = None) -> ReminderRun:
        now = now or datetime.now(UTC)
        settings = self._settings
        if not is_within_send_hours(
            now, settings.outbound_send_hour_start, settings.outbound_send_hour_end
        ):
            return ReminderRun(reason="outside_send_hours")
        windows = settings.outbound_reminder_windows
        horizon = timedelta(hours=max(windows)) if windows else timedelta(0)
        sent = skipped = 0
        for event in await self._repo.upcoming_events(now, horizon):
            hours = due_reminder_window(event.starts_at, now, windows)
            if hours is None:
                continue
            for target in await self._repo.targets_for_event(event):
                if await self.send_reminder(event, target, hours):
                    sent += 1
                else:
                    skipped += 1
        if sent:
            logger.info("outbound.reminders_sent", sent=sent, skipped=skipped)
        return ReminderRun(sent=sent, skipped=skipped)

    async def send_reminder(self, event: Event, target: ReminderTarget, hours: int | str) -> bool:
        """One reminder to one entry. `hours` labels the dedupe key (int or 'manual:<ts>')."""
        local = to_business_time(event.starts_at)
        conversation = target.conversation
        result = await self._templates.send(
            SendRequest(
                organization_id=event.organization_id,
                wa_id=conversation.external_id,
                template=RECORDATORIO_EVENTO,
                variables=[
                    first_name(conversation.full_name),
                    event.nombre,
                    f"{local:%d/%m/%Y}",
                    f"{local:%H:%M}",
                    event.location or _NO_LOCATION,
                ],
                purpose=PURPOSE_EVENT_REMINDER,
                conversation_id=conversation.id,
                card_id=target.card_id,
                agent_id=conversation.instance.agent_id,
                dedupe_key=f"reminder:{event.id}:{target.entry_id}:{hours}h",
                # manual sends carry a timestamp in `hours`, so they never collide
            )
        )
        return result.sent
