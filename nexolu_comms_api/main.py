"""Punto de entrada del servicio: `uvicorn nexolu_comms_api.main:app`."""
from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager, suppress
from pathlib import Path

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles

from nexolu_comms_api.api import panel, webhooks
from nexolu_comms_api.api.v1 import (
    admin_alerts,
    admin_apps,
    admin_catalog,
    admin_channels,
    admin_chats,
    admin_flows,
    admin_media,
    admin_providers,
    admin_quick_replies,
    admin_templates,
    admin_users,
    admin_webhooks,
    catalog,
    embed,
    flows,
    health,
    instagram,
    notifications,
    onboarding,
    usage,
    whatsapp,
)
from nexolu_comms_api.config import get_settings
from nexolu_comms_api.core.alerts import alert_worker_loop
from nexolu_comms_api.core.db.session import init_models
from nexolu_comms_api.core.flows.engine import flow_resume_worker_loop
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
    workers: list[asyncio.Task] = []
    if settings.webhook_retry_worker_enabled:
        workers.append(asyncio.create_task(retry_worker_loop()))
    # Reanuda los nodos `delay` de los flujos vencidos. Ver core/flows/engine.py.
    if settings.flow_resume_worker_enabled:
        workers.append(asyncio.create_task(flow_resume_worker_loop()))
    # Avisa (agrupado) de las conversaciones que llevan rato sin responder:
    # el panel solo avisa mientras alguien lo tiene abierto. Ver core/alerts.py.
    if settings.inbox_alert_worker_enabled:
        workers.append(asyncio.create_task(alert_worker_loop()))

    yield

    for task in workers:
        task.cancel()
        with suppress(asyncio.CancelledError):
            await task


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
    app.include_router(admin_flows.router)
    app.include_router(admin_catalog.router)
    app.include_router(admin_media.router)
    app.include_router(admin_chats.router)
    app.include_router(admin_alerts.router)
    app.include_router(admin_quick_replies.router)
    app.include_router(flows.router)
    app.include_router(catalog.router)
    app.include_router(embed.router)

    # La multimedia subida desde el panel, servida publica: Meta descarga
    # las imagenes de los flujos desde aca (ver api/v1/admin_media.py).
    media_dir = Path(settings.media_dir)
    media_dir.mkdir(parents=True, exist_ok=True)
    app.mount("/media", StaticFiles(directory=str(media_dir)), name="media")

    return app


app = create_app()
