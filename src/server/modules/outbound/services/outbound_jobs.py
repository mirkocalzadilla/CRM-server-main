"""Periodic runner for business-initiated sends, hosted by the worker process.

One tick every `outbound_job_interval_seconds`: each job opens its own session and
commits on success. A failing job is reported and never stops the loop or the other
jobs — the next tick retries, and `dedupe_key` keeps retries harmless.
"""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable

import sentry_sdk
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from server.config import get_settings
from server.modules.agent.services.whatsapp_service import WhatsAppSender
from server.modules.outbound.services.event_reminder_service import EventReminderService
from server.modules.outbound.services.reactivation_service import ReactivationService
from server.modules.outbound.services.template_sender import TemplateSenderPort
from server.shared.database import async_session_maker
from server.shared.logger import get_logger

logger = get_logger(__name__)

Job = Callable[[AsyncSession, TemplateSenderPort], Awaitable[object]]


async def _reminders(session: AsyncSession, sender: TemplateSenderPort) -> object:
    return await EventReminderService(session, sender).run()


async def _reactivation(session: AsyncSession, sender: TemplateSenderPort) -> object:
    return await ReactivationService(session, sender).run()


DEFAULT_JOBS: dict[str, Job] = {"event_reminders": _reminders, "reactivation": _reactivation}


async def run_jobs_once(
    session_maker: async_sessionmaker[AsyncSession],
    sender: TemplateSenderPort,
    jobs: dict[str, Job] = DEFAULT_JOBS,
) -> dict[str, bool]:
    """Runs every job in its own session. Returns {job_name: succeeded}."""
    outcome: dict[str, bool] = {}
    for name, job in jobs.items():
        try:
            async with session_maker() as session:
                await job(session, sender)
                await session.commit()
            outcome[name] = True
        except Exception as exc:
            sentry_sdk.capture_exception(exc)
            logger.error("outbound.job_failed", job=name, error=str(exc))
            outcome[name] = False
    return outcome


async def run_outbound_jobs(
    stop: asyncio.Event,
    session_maker: async_sessionmaker[AsyncSession] = async_session_maker,
    sender: TemplateSenderPort | None = None,
    interval_seconds: int | None = None,
    jobs: dict[str, Job] = DEFAULT_JOBS,
) -> None:
    """Loop: run all jobs, then wait `interval` or until `stop` is set."""
    interval = interval_seconds or get_settings().outbound_job_interval_seconds
    port: TemplateSenderPort = sender if sender is not None else WhatsAppSender()
    logger.info("worker.outbound_started", interval_seconds=interval)
    while not stop.is_set():
        await run_jobs_once(session_maker, port, jobs)
        try:
            await asyncio.wait_for(stop.wait(), timeout=interval)
        except TimeoutError:
            continue
    logger.info("worker.outbound_stopping")
