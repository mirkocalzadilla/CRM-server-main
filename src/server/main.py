import os
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles

from server import __version__
from server.config import get_settings
from server.modules.agent.api.router import agent_module_router
from server.modules.agent.api.webhook_router import router as webhook_router
from server.modules.core.api.health_router import router as health_router
from server.modules.core.api.router import core_router
from server.modules.crm.api.router import router as crm_router
from server.modules.outbound.api.router import router as outbound_router
from server.shared.database import dispose_engine
from server.shared.dispatcher import dispose_dispatcher
from server.shared.handlers import register_exception_handlers
from server.shared.logger import configure_logging, get_logger
from server.shared.observability import init_sentry

API_PREFIX = "/api/v1"


@asynccontextmanager
async def lifespan(_: FastAPI) -> AsyncIterator[None]:
    configure_logging()
    logger = get_logger("startup")
    settings = get_settings()
    logger.info("app_starting", env=settings.app_env, version=__version__)
    yield
    await dispose_engine()
    await dispose_dispatcher()
    logger.info("app_stopped")


def create_app() -> FastAPI:
    settings = get_settings()
    init_sentry(settings)
    os.makedirs(settings.media_root, exist_ok=True)

    app = FastAPI(
        title=settings.app_name,
        version=__version__,
        description="Backend FastAPI para plataforma de marketing con IA",
        docs_url="/docs",
        redoc_url="/redoc",
        openapi_url="/openapi.json",
        lifespan=lifespan,
    )

    app.add_middleware(
        CORSMiddleware,
        allow_origins=settings.cors_origins_list,
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
    )

    register_exception_handlers(app)

    app.include_router(core_router, prefix=API_PREFIX)
    app.include_router(agent_module_router, prefix=API_PREFIX)
    app.include_router(crm_router, prefix=API_PREFIX)
    app.include_router(outbound_router, prefix=API_PREFIX)  # /outbound (M-Outbound)
    app.include_router(webhook_router)  # sin prefijo: /webhooks/whatsapp
    app.include_router(health_router)  # sin prefijo: /health y /health/deep (#246)
    app.mount("/media", StaticFiles(directory=settings.media_root), name="media")

    return app


app = create_app()
