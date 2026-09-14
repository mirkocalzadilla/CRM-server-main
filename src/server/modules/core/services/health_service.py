"""Health profundo (#246): checks reales de dependencias con timeout y cache anti-abuso.

Checks: SELECT 1 a Postgres, PING a Redis y heartbeat del worker (key con TTL que el
loop del worker refresca — ver `server.shared.dispatcher`). Cada check corre con
`asyncio.wait_for`: una dependencia colgada degrada el status, nunca cuelga el endpoint.

El snapshot se cachea in-process `CACHE_TTL_SECONDS`: martillar `/health/deep` cuesta
como máximo un ping a DB/Redis por ventana, sin rate limit en el edge (Caddy stock no
trae módulo de rate limit).
"""

from __future__ import annotations

import asyncio
import time
from collections.abc import Coroutine
from dataclasses import dataclass
from typing import Any

from sqlalchemy import text

from server.shared.database import engine
from server.shared.dispatcher import WORKER_HEARTBEAT_KEY, redis_client
from server.shared.logger import get_logger

logger = get_logger(__name__)

CHECK_TIMEOUT_SECONDS = 2.0
CACHE_TTL_SECONDS = 5.0


@dataclass(frozen=True)
class HealthSnapshot:
    database: bool
    redis: bool
    worker: bool

    @property
    def healthy(self) -> bool:
        return self.database and self.redis and self.worker


async def _check_database() -> bool:
    async with engine.connect() as conn:
        await conn.execute(text("SELECT 1"))
    return True


async def _check_redis() -> bool:
    await redis_client.ping()
    return True


async def _check_worker() -> bool:
    # El worker refresca la key con TTL en cada iteración de su loop; si expiró, está caído
    # (o lleva más de WORKER_HEARTBEAT_TTL sin iterar, que operativamente es lo mismo).
    return bool(await redis_client.exists(WORKER_HEARTBEAT_KEY))


class HealthService:
    def __init__(self, cache_ttl: float = CACHE_TTL_SECONDS) -> None:
        self._cache_ttl = cache_ttl
        self._cached: HealthSnapshot | None = None
        self._cached_at = 0.0

    async def snapshot(self) -> HealthSnapshot:
        if self._cached is not None and time.monotonic() - self._cached_at < self._cache_ttl:
            return self._cached
        database, redis_ok, worker = await asyncio.gather(
            _guarded(_check_database(), "database"),
            _guarded(_check_redis(), "redis"),
            _guarded(_check_worker(), "worker"),
        )
        snapshot = HealthSnapshot(database=database, redis=redis_ok, worker=worker)
        self._cached = snapshot
        self._cached_at = time.monotonic()
        if not snapshot.healthy:
            logger.warning("health.degraded", database=database, redis=redis_ok, worker=worker)
        return snapshot

    def reset(self) -> None:
        """Invalida el cache — solo para tests."""
        self._cached = None


async def _guarded(check: Coroutine[Any, Any, bool], name: str) -> bool:
    try:
        return await asyncio.wait_for(check, timeout=CHECK_TIMEOUT_SECONDS)
    except Exception as exc:
        logger.warning("health.check_failed", check=name, error=str(exc))
        return False


health_service = HealthService()
