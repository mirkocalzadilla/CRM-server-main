"""Rutas de health (#246) — montadas SIN prefijo `/api/v1` (contrato con compose/Sentry).

- `/health`: liveness puro, público y mínimo. Consumidor: healthcheck de docker-compose
  (cada 10s, solo mira el 200). No toca dependencias: un blip de DB no debe reiniciar la
  app ni gatillar un rollback falso del deploy.
- `/health/deep`: readiness — DB + Redis + worker vía `HealthService` (cache 5s). 200 sano,
  503 degradado. Público sanitizado (booleanos): es lo que consume el uptime monitor de
  Sentry sin credenciales. `sha`/`version` solo con `Authorization: Bearer $HEALTH_TOKEN`
  (recon: con el repo exfiltrado, el sha público mapea exactamente qué corre en prod).
"""

from __future__ import annotations

import secrets

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse

from server import __version__
from server.config import get_settings
from server.modules.core.services.health_service import health_service

router = APIRouter(tags=["health"])


@router.get("/health")
async def health_check() -> dict[str, str]:
    return {"status": "ok"}


@router.get("/health/deep")
async def health_deep(request: Request) -> JSONResponse:
    snapshot = await health_service.snapshot()
    body: dict[str, object] = {
        "status": "ok" if snapshot.healthy else "degraded",
        "checks": {
            "database": snapshot.database,
            "redis": snapshot.redis,
            "worker": snapshot.worker,
        },
    }
    if _bearer_token_valid(request.headers.get("authorization")):
        body["sha"] = get_settings().git_sha
        body["version"] = __version__
    return JSONResponse(
        content=body,
        status_code=200 if snapshot.healthy else 503,
        headers={"Cache-Control": "no-store"},
    )


def _bearer_token_valid(header: str | None) -> bool:
    """Token inválido/ausente NO es 401: la respuesta sanitizada sigue siendo válida."""
    token = get_settings().health_token
    if not token or not header or not header.lower().startswith("bearer "):
        return False
    return secrets.compare_digest(header[len("bearer ") :].strip(), token)
