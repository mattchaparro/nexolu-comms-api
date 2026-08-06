"""Acceso a datos de `Notification`: registrar envios y agregar uso/costo.

Las agregaciones (`usage_daily_series`/`usage_by_business`/`usage_by_app`)
son consultas SQL directas sobre `notifications`, no una tabla de rollup
aparte: al volumen que maneja un servicio nuevo, calcular en el momento es
mas simple de mantener que mantener sincronizada una segunda tabla. Si el
volumen algun dia lo justifica, agregar un rollup es un cambio localizado
aca, sin tocar los endpoints que lo consumen.
"""
from __future__ import annotations

from collections.abc import Sequence
from datetime import date

from sqlalchemy import Date, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from nexolu_comms_api.core.db.entities import Notification


class NotificationRepository:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    def log(
        self,
        *,
        app_id: str,
        business_id: str,
        channel: str,
        recipient: str,
        status: str,
        reference: str | None = None,
        provider_message_id: str | None = None,
        error: str | None = None,
        cost_micros: int | None = None,
    ) -> Notification:
        """No hace commit: el caller decide cuando (p.ej. una sola vez tras
        registrar todos los canales de un mismo POST /v1/notifications/send)."""
        notification = Notification(
            app_id=app_id,
            business_id=business_id,
            reference=reference,
            channel=channel,
            recipient=recipient,
            status=status,
            provider_message_id=provider_message_id,
            error=error,
            cost_micros=cost_micros,
        )
        self._session.add(notification)
        return notification

    async def usage_daily_series(
        self,
        *,
        app_id: str,
        business_id: str | None,
        channel: str | None,
        date_from: date,
        date_to: date,
    ) -> Sequence[tuple[date, int, int]]:
        """@return [(date, message_count, cost_micros), ...] ordenado por fecha."""
        # func.date(), no cast(..., Date): CAST(col AS DATE) no trunca de
        # forma confiable el DateTime en SQLite (rompe el procesador de tipo
        # Date de SQLAlchemy al leer el resultado) - func.date() mapea al
        # DATE()/date() nativo de MySQL y SQLite por igual.
        day = func.date(Notification.created_at, type_=Date)
        query = (
            select(day.label("day"), func.count(Notification.id), func.coalesce(func.sum(Notification.cost_micros), 0))
            .where(Notification.app_id == app_id)
            .where(day >= date_from)
            .where(day <= date_to)
            .where(Notification.status == "sent")
            .group_by(day)
            .order_by(day)
        )
        if business_id is not None:
            query = query.where(Notification.business_id == business_id)
        if channel is not None:
            query = query.where(Notification.channel == channel)

        result = await self._session.execute(query)
        return result.all()

    async def usage_by_business(
        self, *, app_id: str, date_from: date, date_to: date
    ) -> Sequence[tuple[str, int, int]]:
        return await self._grouped_usage(Notification.business_id, app_id=app_id, date_from=date_from, date_to=date_to)

    async def usage_by_app(self, *, date_from: date, date_to: date) -> Sequence[tuple[str, int, int]]:
        return await self._grouped_usage(Notification.app_id, app_id=None, date_from=date_from, date_to=date_to)

    async def _grouped_usage(
        self, key_column, *, app_id: str | None, date_from: date, date_to: date
    ) -> Sequence[tuple[str, int, int]]:
        day = func.date(Notification.created_at, type_=Date)
        query = (
            select(key_column, func.count(Notification.id), func.coalesce(func.sum(Notification.cost_micros), 0))
            .where(day >= date_from)
            .where(day <= date_to)
            .where(Notification.status == "sent")
            .group_by(key_column)
            .order_by(key_column)
        )
        if app_id is not None:
            query = query.where(Notification.app_id == app_id)

        result = await self._session.execute(query)
        return result.all()
