"""Acceso a `asset` — materiales subidos, siempre org-scoped (multi-tenant)."""

from __future__ import annotations

import uuid

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from server.modules.agent.domain.catalog_models import Asset


class AssetRepository:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def get(self, asset_id: uuid.UUID, organization_id: uuid.UUID) -> Asset | None:
        result = await self._session.execute(
            select(Asset).where(Asset.id == asset_id, Asset.organization_id == organization_id)
        )
        return result.scalar_one_or_none()

    async def list_for_category(self, category_id: uuid.UUID) -> list[Asset]:
        result = await self._session.execute(
            select(Asset).where(Asset.category_id == category_id).order_by(Asset.created_at)
        )
        return list(result.scalars().all())

    async def add(self, asset: Asset) -> Asset:
        self._session.add(asset)
        await self._session.flush()
        await self._session.refresh(asset)
        return asset

    async def delete(self, asset: Asset) -> None:
        await self._session.delete(asset)
        await self._session.flush()
