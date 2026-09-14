"""Backfill de `service.price_amount` desde el texto de `service.precio` (server#268).

Se corre **una vez** después de aplicar la migración `0027_catalog_delivery`, y es
**idempotente**: solo toca filas cuyo `price_amount` está en NULL, así que re-correrlo
no pisa nada que el operador haya cargado a mano.

Qué hace y qué no:

- **Sí:** parsea el texto display a un monto canónico cuando el valor es inequívoco
  ("650 Bs" → 650.00, "5.500 Bs" → 5500.00: en es-BO el punto es separador de miles).
- **No:** inventar un monto cuando el precio es un rango o multi-ítem ("1.800 / 3.000",
  "desde 3.800 / 2.800 / 450"). Esas filas quedan en NULL, que es exactamente lo que
  manda un comprobante de ese servicio a validación humana.
- **No:** adivinar la modalidad. `service.modality` queda NULL (= sin entrega
  automática) y la marca el operador desde el CRM. Mandarle una entrada a alguien que
  compró otra cosa es peor que no mandarle nada.

Uso (dentro del contenedor, como el resto de los scripts):

    docker compose exec backend python scripts/backfill_catalog_prices.py          # aplica
    docker compose exec backend python scripts/backfill_catalog_prices.py --dry-run  # solo reporta

Deshacer: `UPDATE service SET price_amount = NULL;` (el texto display nunca se toca).
"""

from __future__ import annotations

import asyncio
import sys

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from server.modules.agent.domain import models as _agent_models  # noqa: F401  (registra `agent`)
from server.modules.agent.domain.catalog_models import Service
from server.modules.crm.domain.money import parse_amount
from server.shared.database import async_session_maker, dispose_engine
from server.shared.logger import configure_logging, get_logger

logger = get_logger(__name__)


async def backfill(session: AsyncSession, *, dry_run: bool) -> tuple[int, int]:
    """Devuelve (filas actualizadas, filas que quedaron sin monto)."""
    result = await session.execute(
        select(Service).where(Service.price_amount.is_(None)).order_by(Service.orden, Service.slug)
    )
    services = list(result.scalars().unique().all())

    filled = 0
    skipped = 0
    for service in services:
        amount = parse_amount(service.precio)
        if amount is None:
            skipped += 1
            logger.info(
                "backfill.skipped",
                slug=service.slug,
                precio=service.precio,
                moneda=service.moneda,
                reason="precio no determina un monto único (rango, multi-ítem o texto)",
            )
            continue
        filled += 1
        logger.info(
            "backfill.filled",
            slug=service.slug,
            precio=service.precio,
            moneda=service.moneda,
            price_amount=str(amount),
        )
        if not dry_run:
            service.price_amount = amount

    if dry_run:
        await session.rollback()
    else:
        await session.commit()
    return filled, skipped


async def main() -> None:
    configure_logging()
    dry_run = "--dry-run" in sys.argv
    async with async_session_maker() as session:
        filled, skipped = await backfill(session, dry_run=dry_run)
    await dispose_engine()
    logger.info("backfill.done", dry_run=dry_run, filled=filled, skipped=skipped)
    print(
        f"{'[dry-run] ' if dry_run else ''}price_amount cargado en {filled} servicio(s); "
        f"{skipped} quedaron sin monto (validación humana). "
        "La modalidad NO se toca: cargala desde el CRM."
    )


if __name__ == "__main__":
    asyncio.run(main())
