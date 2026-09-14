"""Tests de /health (liveness mínimo) y /health/deep (#246).

Los checks reales (Postgres/Redis/heartbeat) se monkeypatchean a nivel módulo: acá se
prueba el contrato HTTP (status codes, sanitizado vs token) y la mecánica del service
(cache anti-abuso, timeout), no la conectividad.
"""

from __future__ import annotations

import asyncio

import pytest
from httpx import AsyncClient

import server.modules.core.services.health_service as health_module
from server.config import get_settings
from server.modules.core.services.health_service import HealthService, health_service


async def _ok() -> bool:
    return True


async def _down() -> bool:
    raise ConnectionError("dependency down")


@pytest.fixture(autouse=True)
def _fresh_health_state(monkeypatch: pytest.MonkeyPatch) -> None:
    """Cache limpio + checks sanos por defecto; cada test pisa lo que necesita."""
    health_service.reset()
    monkeypatch.setattr(health_module, "_check_database", _ok)
    monkeypatch.setattr(health_module, "_check_redis", _ok)
    monkeypatch.setattr(health_module, "_check_worker", _ok)


async def test_health_is_minimal_liveness(client: AsyncClient) -> None:
    resp = await client.get("/health")
    assert resp.status_code == 200
    # Sin sha ni version: el sha público era recon (#246); solo status para compose/uptime.
    assert resp.json() == {"status": "ok"}


async def test_deep_ok_is_sanitized_without_token(client: AsyncClient) -> None:
    resp = await client.get("/health/deep")
    assert resp.status_code == 200
    body = resp.json()
    assert body == {
        "status": "ok",
        "checks": {"database": True, "redis": True, "worker": True},
    }
    assert resp.headers["cache-control"] == "no-store"


async def test_deep_degraded_when_database_down(
    client: AsyncClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(health_module, "_check_database", _down)
    resp = await client.get("/health/deep")
    assert resp.status_code == 503
    body = resp.json()
    assert body["status"] == "degraded"
    assert body["checks"] == {"database": False, "redis": True, "worker": True}


async def test_deep_degraded_when_worker_heartbeat_missing(
    client: AsyncClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def no_beat() -> bool:
        return False

    monkeypatch.setattr(health_module, "_check_worker", no_beat)
    resp = await client.get("/health/deep")
    assert resp.status_code == 503
    assert resp.json()["checks"]["worker"] is False


async def test_deep_exposes_sha_with_valid_token(
    client: AsyncClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(get_settings(), "health_token", "s3cret")
    resp = await client.get("/health/deep", headers={"Authorization": "Bearer s3cret"})
    assert resp.status_code == 200
    body = resp.json()
    assert body["sha"] == "unknown"  # tests/local sin GIT_SHA; en prod es el commit del build
    assert "version" in body


async def test_deep_wrong_token_stays_sanitized(
    client: AsyncClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(get_settings(), "health_token", "s3cret")
    resp = await client.get("/health/deep", headers={"Authorization": "Bearer nope"})
    # Token errado NO es 401: misma respuesta pública sanitizada.
    assert resp.status_code == 200
    assert "sha" not in resp.json()


async def test_deep_never_exposes_sha_without_configured_token(client: AsyncClient) -> None:
    # health_token vacío (default): ni un Bearer cualquiera habilita el sha.
    resp = await client.get("/health/deep", headers={"Authorization": "Bearer anything"})
    assert "sha" not in resp.json()


async def test_snapshot_is_cached_within_ttl(monkeypatch: pytest.MonkeyPatch) -> None:
    calls = 0

    async def counting() -> bool:
        nonlocal calls
        calls += 1
        return True

    monkeypatch.setattr(health_module, "_check_database", counting)
    service = HealthService(cache_ttl=60)
    await service.snapshot()
    await service.snapshot()
    assert calls == 1  # segunda lectura sale del cache: martillar no toca la DB

    service_no_cache = HealthService(cache_ttl=0)
    await service_no_cache.snapshot()
    await service_no_cache.snapshot()
    assert calls == 3


async def test_hung_check_times_out_as_degraded(monkeypatch: pytest.MonkeyPatch) -> None:
    async def hung() -> bool:
        await asyncio.sleep(5)
        return True

    monkeypatch.setattr(health_module, "_check_database", hung)
    monkeypatch.setattr(health_module, "CHECK_TIMEOUT_SECONDS", 0.05)
    snapshot = await HealthService(cache_ttl=0).snapshot()
    assert snapshot.database is False  # cuelga la dependencia, no el endpoint
    assert snapshot.redis is True
    assert snapshot.healthy is False
