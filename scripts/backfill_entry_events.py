"""Liga al evento las entradas emitidas antes de que los eventos existieran (server#276).

Se corre **después** de crear los eventos, y solo asigna cuando **no hay ambigüedad**:
si el servicio de la card tiene exactamente un evento, esa entrada es de ese evento. Con
dos o más no se adivina — mandar a alguien a la fecha equivocada es peor que dejar la
entrada como legacy, que el escáner acepta con una advertencia.

Idempotente: solo toca entradas con `event_id` en NULL.

Uso:

    docker compose exec backend python scripts/backfill_entry_events.py --dry-run
    docker compose exec backend python scripts/backfill_entry_events.py

Deshacer: `UPDATE qr_entry SET event_id = NULL, event_snapshot = '{}';`
"""

from __future__ import annotations

import asyncio
import sys

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from server.modules.agent.domain import models as _agent_models  # noqa: F401  (registra `agent`)
from server.modules.crm.domain.event_models import Event
from server.modules.crm.domain.models import Card, QrEntry
from server.modules.crm.repositories.card_delivery_repository import CardDeliveryRepository
from server.modules.crm.services.event_service import snapshot_of
from server.shared.database import async_session_maker, dispose_engine
from server.shared.logger import configure_logging, get_logger

logger = get_logger(__name__)


async def backfill(session: AsyncSession, *, dry_run: bool) -> tuple[int, int]:
    """Devuelve (entradas ligadas, entradas que quedaron legacy)."""
    entries = list(
        (await session.execute(select(QrEntry).where(QrEntry.event_id.is_(None)))).scalars().all()
    )
    linked = 0
    skipped = 0
    for entry in entries:
        card = await session.get(Card, entry.card_id)
        if card is None:
            skipped += 1
            continue
        services = await CardDeliveryRepository(session).services_for_card(
            card.id, card.organization_id
        )
        if len(services) != 1:
            skipped += 1
            logger.info(
                "backfill.skipped", entry=str(entry.id), reason="la card no tiene un servicio único"
            )
            continue
        events = list(
            (
                await session.execute(
                    select(Event).where(
                        Event.service_id == services[0].id,
                        Event.organization_id == card.organization_id,
                    )
                )
            )
            .scalars()
            .all()
        )
        if len(events) != 1:
            skipped += 1
            logger.info(
                "backfill.skipped",
                entry=str(entry.id),
                reason=f"el servicio tiene {len(events)} eventos: no se adivina",
            )
            continue
        linked += 1
        logger.info("backfill.linked", entry=str(entry.id), event=str(events[0].id))
        if not dry_run:
            entry.event_id = events[0].id
            entry.event_snapshot = snapshot_of(events[0])

    if dry_run:
        await session.rollback()
    else:
        await session.commit()
    return linked, skipped


async def main() -> None:
    configure_logging()
    dry_run = "--dry-run" in sys.argv
    async with async_session_maker() as session:
        linked, skipped = await backfill(session, dry_run=dry_run)
    await dispose_engine()
    logger.info("backfill.done", dry_run=dry_run, linked=linked, skipped=skipped)
    print(
        f"{'[dry-run] ' if dry_run else ''}{linked} entrada(s) ligadas a su evento; "
        f"{skipped} quedaron como legacy (el escáner las acepta con advertencia)."
    )


if __name__ == "__main__":
    asyncio.run(main())
