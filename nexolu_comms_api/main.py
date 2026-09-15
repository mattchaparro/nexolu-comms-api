"""Punto de entrada del servicio: `uvicorn nexolu_comms_api.main:app`."""
from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager, suppress

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from nexolu_comms_api.api import panel, webhooks
from nexolu_comms_api.api.v1 import (
    admin_apps,
    admin_channels,
    admin_providers,
    admin_templates,
    admin_users,
    admin_webhooks,
    health,
    instagram,
    notifications,
    onboarding,
    usage,
    whatsapp,
)
from nexolu_comms_api.config import get_settings
from nexolu_comms_api.core.db.session import init_models
from nexolu_comms_api.core.telemetry.logging import configure_logging
from nexolu_comms_api.core.webhooks.forwarder import retry_worker_loop


@asynccontextmanager
async def lifespan(app: FastAPI):
    settings = get_settings()
    configure_logging(settings.log_level)

    # Autocrear tablas solo tiene sentido en SQLite de desarrollo. En
    # produccion (MySQL) el esquema se maneja con `alembic upgrade head`,
    # corrido como parte del despliegue, no al arrancar el proceso.
    if settings.database_url.startswith("sqlite"):
        await init_models()

    # Worker de reintento de webhooks: task del propio proceso, cancelada
    # limpiamente al apagar. Ver core/webhooks/forwarder.py.
    retry_task: asyncio.Task | None = None
    if settings.webhook_retry_worker_enabled:
        retry_task = asyncio.create_task(retry_worker_loop())

    yield

    if retry_task is not None:
        retry_task.cancel()
        with suppress(asyncio.CancelledError):
            await retry_task


def create_app() -> FastAPI:
    app = FastAPI(
        title="Nexolu Communications",
        description="Envio de WhatsApp y correo centralizado para todo el ecosistema Nexolu.",
        version="0.1.0",
        lifespan=lifespan,
    )

    settings = get_settings()
    # CORS solo para el panel dedicado (nexolu-comms-front): las apps
    # server-side no pasan por un navegador y no lo necesitan.
    if settings.panel_cors_origins:
        app.add_middleware(
            CORSMiddleware,
            allow_origins=[o.strip() for o in settings.panel_cors_origins.split(",") if o.strip()],
            allow_credentials=False,  # el token viaja en Authorization, no en cookies
            allow_methods=["*"],
            allow_headers=["*"],
        )

    app.include_router(health.router)
    app.include_router(notifications.router)
    app.include_router(usage.router)
    app.include_router(whatsapp.router)
    app.include_router(instagram.router)
    app.include_router(webhooks.router)
    app.include_router(panel.router)
    app.include_router(onboarding.router)
    app.include_router(admin_apps.router)
    app.include_router(admin_providers.router)
    app.include_router(admin_webhooks.router)
    app.include_router(admin_channels.router)
    app.include_router(admin_users.router)
    app.include_router(admin_templates.router)

    return app


app = create_app()
