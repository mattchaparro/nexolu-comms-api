"""Consulta de uso/costo agregado.

El registro de uso ya ocurre dentro de `POST /v1/notifications/send` (ver
`api/v1/notifications.py`), siempre, sin condicionarlo a ningun plan
comercial. Este modulo es el lado de lectura, con dos audiencias distintas
para el mismo dato crudo (`Notification`):

- Una app integradora ve SU propio gasto (opcionalmente por negocio y por
  canal) via `summary`/`by_business`/`daily_series` - autenticada con su
  propia API key, nunca ve datos de otra app.
- Nexolu como plataforma ve el gasto de TODAS las apps via `by_app` -
  autenticada aparte (ver `require_platform_access`).
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import date

from nexolu_comms_api.core.db.repository import NotificationRepository


@dataclass(frozen=True)
class UsageSummary:
    message_count: int
    cost_usd: float


@dataclass(frozen=True)
class UsageBreakdown:
    """Un UsageSummary con la clave (business_id o app_id) que lo agrupa."""

    key: str
    summary: UsageSummary


@dataclass(frozen=True)
class UsageDailyPoint:
    date: date
    summary: UsageSummary


def _summary(message_count: int, cost_micros: int) -> UsageSummary:
    return UsageSummary(message_count=message_count or 0, cost_usd=(cost_micros or 0) / 1_000_000)


class UsageService:
    def __init__(self, repository: NotificationRepository) -> None:
        self._repo = repository

    async def summary(
        self, *, app_id: str, business_id: str | None, channel: str | None, date_from: date, date_to: date
    ) -> UsageSummary:
        rows = await self._repo.usage_daily_series(
            app_id=app_id, business_id=business_id, channel=channel, date_from=date_from, date_to=date_to
        )
        return UsageSummary(
            message_count=sum(r[1] for r in rows),
            cost_usd=sum(r[2] for r in rows) / 1_000_000,
        )

    async def daily_series(
        self, *, app_id: str, business_id: str | None, channel: str | None, date_from: date, date_to: date
    ) -> list[UsageDailyPoint]:
        rows = await self._repo.usage_daily_series(
            app_id=app_id, business_id=business_id, channel=channel, date_from=date_from, date_to=date_to
        )
        return [UsageDailyPoint(date=r[0], summary=_summary(r[1], r[2])) for r in rows]

    async def by_business(self, *, app_id: str, date_from: date, date_to: date) -> list[UsageBreakdown]:
        rows = await self._repo.usage_by_business(app_id=app_id, date_from=date_from, date_to=date_to)
        return [UsageBreakdown(key=key, summary=_summary(count, cost)) for key, count, cost in rows]

    async def by_app(self, *, date_from: date, date_to: date) -> list[UsageBreakdown]:
        rows = await self._repo.usage_by_app(date_from=date_from, date_to=date_to)
        return [UsageBreakdown(key=key, summary=_summary(count, cost)) for key, count, cost in rows]
