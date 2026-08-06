"""Motor y sesiones de base de datos, async de punta a punta.

`DATABASE_URL` decide el motor: SQLite (`sqlite+aiosqlite:///...`) para
desarrollo local y tests sin infraestructura, MySQL
(`mysql+aiomysql://...`) en produccion. Cambiar de motor es cambiar una env
var, no codigo.
"""
from __future__ import annotations

from collections.abc import AsyncIterator
from functools import lru_cache

from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)
from sqlalchemy.orm import DeclarativeBase

from nexolu_comms_api.config import Settings, get_settings


class Base(DeclarativeBase):
    pass


@lru_cache
def get_engine() -> AsyncEngine:
    settings: Settings = get_settings()
    return create_async_engine(settings.database_url, echo=False)


@lru_cache
def get_sessionmaker() -> async_sessionmaker[AsyncSession]:
    return async_sessionmaker(get_engine(), expire_on_commit=False)


async def get_session() -> AsyncIterator[AsyncSession]:
    """Dependencia de FastAPI: una sesion por request."""
    async with get_sessionmaker()() as session:
        yield session


async def init_models() -> None:
    """Crea las tablas si no existen. Util para desarrollo local y tests con
    SQLite; en produccion con MySQL el esquema se maneja con Alembic (ver
    alembic/)."""
    async with get_engine().begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
