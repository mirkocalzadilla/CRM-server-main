"""Sentry init (error tracking). No-op si falta SENTRY_DSN.

Cobertura C1: la integración de FastAPI captura las 5xx no manejadas; los puntos que hoy
tragan la excepción (webhook a Meta, dispatch del worker) llaman a `capture_exception`.
`LoggingIntegration` queda en `event_level=None` para no duplicar eventos desde los logs.

PII: nunca se envía el contenido de los mensajes del cliente — `send_default_pii=False`,
`include_local_variables=False` y un `before_send` que ofusca claves sensibles.
"""

from __future__ import annotations

import logging
from typing import cast

import sentry_sdk
from sentry_sdk.integrations.logging import LoggingIntegration
from sentry_sdk.types import Event, Hint

from server.config import Settings

# Claves cuyo valor se ofusca antes de mandar el evento (datos del cliente / secretos).
_SENSITIVE_KEYS = frozenset(
    {
        "text",
        "body",
        "message",
        "caption",
        "wa_id",
        "phone",
        "from",
        "to",
        "anthropic_api_key",
        "openai_api_key",
        "whatsapp_access_token",
        "whatsapp_app_secret",
        "jwt_secret_key",
        "authorization",
        "password",
    }
)
_REDACTED = "[redacted]"


def _scrub(value: object) -> object:
    """Ofusca recursivamente los valores de claves sensibles."""
    if isinstance(value, dict):
        return {
            k: _REDACTED if str(k).lower() in _SENSITIVE_KEYS else _scrub(v)
            for k, v in value.items()
        }
    if isinstance(value, list):
        return [_scrub(v) for v in value]
    return value


def _before_send(event: Event, _hint: Hint) -> Event | None:
    return cast("Event", _scrub(event))


def init_sentry(settings: Settings) -> bool:
    """Inicializa Sentry si hay DSN configurado. Devuelve True si quedó activo.

    Se llama al arranque de la app (`main`) y del worker (proceso aparte).
    """
    if not settings.sentry_dsn:
        return False

    # Release: SENTRY_RELEASE explicito gana; si falta, el sha del build que el CI
    # bakea en la imagen (el mismo que expone /health). "unknown" = build local sin sha.
    release = settings.sentry_release or settings.git_sha
    sentry_sdk.init(
        dsn=settings.sentry_dsn,
        environment=settings.app_env,
        release=release if release != "unknown" else None,
        traces_sample_rate=settings.sentry_traces_sample_rate,
        send_default_pii=False,
        include_local_variables=False,
        integrations=[LoggingIntegration(level=logging.INFO, event_level=None)],
        before_send=_before_send,
    )
    return True
