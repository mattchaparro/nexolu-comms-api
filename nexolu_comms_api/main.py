"""Punto de entrada del servicio: `uvicorn nexolu_comms_api.main:app`."""
from __future__ import annotations

from contextlib import asynccontextmanager

from fastapi import FastAPI

from nexolu_comms_api.api import webhooks
from nexolu_comms_api.api.v1 import health, notifications, usage, whatsapp
from nexolu_comms_api.config import get_settings
from nexolu_comms_api.core.db.session import init_models
from nexolu_comms_api.core.telemetry.logging import configure_logging


@asynccontextmanager
async def lifespan(app: FastAPI):
    settings = get_settings()
    configure_logging(settings.log_level)

    # Autocrear tablas solo tiene sentido en SQLite de desarrollo. En
    # produccion (MySQL) el esquema se maneja con `alembic upgrade head`,
    # corrido como parte del despliegue, no al arrancar el proceso.
    if settings.database_url.startswith("sqlite"):
        await init_models()

    yield


def create_app() -> FastAPI:
    app = FastAPI(
        title="Nexolu Communications",
        description="Envio de WhatsApp y correo centralizado para todo el ecosistema Nexolu.",
        version="0.1.0",
        lifespan=lifespan,
    )

    app.include_router(health.router)
    app.include_router(notifications.router)
    app.include_router(usage.router)
    app.include_router(whatsapp.router)
    app.include_router(webhooks.router)

    return app


app = create_app()
