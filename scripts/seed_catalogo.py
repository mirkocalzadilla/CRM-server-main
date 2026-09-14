"""Carga inicial del catálogo curado de Mirko (SPEC_catalogo_y_materiales §2.2).

Idempotente: upsert por (agent_id, slug). Crea los 8 servicios (3 categorías + 1
programa general) sobre el agente de Mirko (org `mirko`, producto `cursos-mirko`).

PDFs: best-effort. Si existe el directorio fuente (env `CATALOGO_MATERIAL_DIR`,
default = la carpeta `material 20-06-2026` local), copia cada PDF a
`media_root/catalogo/{org}/{slug-pdf}.pdf`, crea/reusa el `asset` y lo enlaza. Si
no está, el servicio queda sin PDF (se sube luego desde la UI). Al final sincroniza
el snapshot del agente (auto-publish, #109): ya no hay paso manual de "publicar".

Uso: uv run python scripts/seed_catalogo.py
"""

from __future__ import annotations

import asyncio
import os
import shutil
import uuid

from catalogo_data import CATEGORIES, CURATED, PDF_SOURCES
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from server.config import get_settings
from server.modules.agent.domain.catalog_models import Asset, Service, ServiceCategory
from server.modules.agent.domain.models import Agent
from server.modules.agent.services.asset_service import sanitize_filename
from server.modules.agent.services.catalog_service import CatalogService
from server.modules.core.repositories.tenant_repository import TenantRepository
from server.shared.database import async_session_maker, dispose_engine
from server.shared.logger import configure_logging, get_logger

logger = get_logger("seed_catalogo")

ORG_SLUG = "mirko"
PRODUCT_SLUG = "cursos-mirko"
DEFAULT_MATERIAL_DIR = r"C:\Users\Natalia\Documents\Documentos landing Mirko\material 20-06-2026"


async def _resolve_agent(session: AsyncSession) -> Agent | None:
    tenant = await TenantRepository(session).get_by_slug(ORG_SLUG)
    if tenant is None:
        return None
    return await session.scalar(
        select(Agent).where(Agent.organization_id == tenant.id, Agent.product_slug == PRODUCT_SLUG)
    )


async def _ensure_asset(session: AsyncSession, org_id: str, slug: str, source: str) -> Asset | None:
    """Copia el PDF fuente (si existe) a media_root y upserta el `asset`."""
    if not os.path.isfile(source):
        return None
    settings = get_settings()
    directory = os.path.join(settings.media_root, "catalogo", org_id)
    os.makedirs(directory, exist_ok=True)
    storage_ref = f"catalogo/{org_id}/{slug}.pdf"
    dest = os.path.join(settings.media_root, storage_ref)
    shutil.copyfile(source, dest)
    existing = await session.scalar(select(Asset).where(Asset.storage_ref == storage_ref))
    public_url = f"{settings.media_base_url}/media/{storage_ref}"
    filename = sanitize_filename(os.path.basename(source))
    if existing is not None:
        existing.public_url, existing.filename, existing.bytes = (
            public_url,
            filename,
            os.path.getsize(dest),
        )
        return existing
    asset = Asset(
        organization_id=uuid.UUID(org_id),
        kind="pdf",
        filename=filename,
        storage_ref=storage_ref,
        public_url=public_url,
        bytes=os.path.getsize(dest),
    )
    session.add(asset)
    await session.flush()
    return asset


async def _ensure_categories(
    session: AsyncSession, org_id: uuid.UUID
) -> dict[str, ServiceCategory]:
    """Upsert idempotente de las 4 categorías (por org, nombre) → {nombre: categoría}."""
    result: dict[str, ServiceCategory] = {}
    for orden, nombre in enumerate(CATEGORIES):
        category = await session.scalar(
            select(ServiceCategory).where(
                ServiceCategory.organization_id == org_id,
                ServiceCategory.nombre == nombre,
            )
        )
        if category is None:
            category = ServiceCategory(organization_id=org_id, nombre=nombre, orden=orden)
            session.add(category)
            await session.flush()
        else:
            category.orden = orden
        result[nombre] = category
    return result


async def _upsert_service(
    session: AsyncSession,
    agent: Agent,
    data: dict[str, object],
    asset: Asset | None,
    orden: int,
    categories: dict[str, ServiceCategory],
) -> None:
    slug = str(data["slug"])
    service = await session.scalar(
        select(Service).where(Service.agent_id == agent.id, Service.slug == slug)
    )
    if service is None:
        service = Service(organization_id=agent.organization_id, agent_id=agent.id, slug=slug)
        session.add(service)
    category = categories.get(str(data["categoria"]))
    service.nombre = str(data["nombre"])
    service.category_id = category.id if category is not None else None
    service.resumen = str(data["resumen"])
    service.detalle = str(data["detalle"]) if data.get("detalle") else None
    service.precio = str(data["precio"])
    service.moneda = str(data["moneda"])
    service.flujo_cierre = str(data["flujo_cierre"])
    service.orden = orden
    service.is_active = True
    if asset is not None:
        await session.flush()  # asegura service.id para enlazar el material (#108)
        asset.service_id = service.id


async def seed() -> None:
    source_dir = os.environ.get("CATALOGO_MATERIAL_DIR", DEFAULT_MATERIAL_DIR)
    async with async_session_maker() as session:
        agent = await _resolve_agent(session)
        if agent is None:
            logger.warning("seed_catalogo.no_agent", org=ORG_SLUG, product=PRODUCT_SLUG)
            return
        org_id = str(agent.organization_id)
        categories = await _ensure_categories(session, agent.organization_id)
        assets: dict[str, Asset | None] = {}
        for key, fname in PDF_SOURCES.items():
            assets[key] = await _ensure_asset(session, org_id, key, os.path.join(source_dir, fname))
        attached = sum(1 for a in assets.values() if a is not None)
        for orden, data in enumerate(CURATED):
            await _upsert_service(
                session, agent, data, assets.get(str(data["pdf"])), orden, categories
            )
        await session.commit()
        # Proyecta el catálogo al snapshot que lee el agente (antes lo hacía el botón
        # de "publicar"; ahora es automático, #109).
        await CatalogService(session).sync_snapshot(agent.id, agent.organization_id)
        logger.info(
            "seed_catalogo.done",
            agent_id=str(agent.id),
            categories=len(categories),
            services=len(CURATED),
            pdfs_attached=attached,
            material_dir=source_dir,
        )


async def main() -> None:
    configure_logging()
    try:
        await seed()
    finally:
        await dispose_engine()


if __name__ == "__main__":
    asyncio.run(main())
