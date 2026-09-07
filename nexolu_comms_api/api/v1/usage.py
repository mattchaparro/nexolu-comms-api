"""Reporte de uso/costo. Dos audiencias, dos niveles de auth (ver
`core/auth/dependencies.py` y `core/telemetry/usage.py` para el porque):

- `GET /v1/usage/summary` y `GET /v1/usage/daily`: una app ve SU propio
  gasto (autenticada con su propia API key), opcionalmente filtrado a un
  negocio y/o canal puntual.
- `GET /v1/platform/usage`: Nexolu ve el gasto de TODAS las apps
  (autenticada con NEXOLU_PLATFORM_API_KEY, nunca entregada a una app).
- `GET /v1/platform/notifications`: Nexolu lista/filtra el detalle crudo de
  envios de TODAS las apps (panel de logs del Admin) - misma auth que arriba.
"""
from __future__ import annotations

from datetime import UTC, date, datetime, timedelta

from fastapi import APIRouter, Depends, Query
from pydantic import BaseModel
from sqlalchemy.ext.asyncio import AsyncSession

from nexolu_comms_api.core.auth.apps import AppIdentity
from nexolu_comms_api.core.auth.dependencies import get_current_app, require_platform_access
from nexolu_comms_api.core.db.repository import NotificationRepository
from nexolu_comms_api.core.db.session import get_session
from nexolu_comms_api.core.telemetry.usage import UsageService

router = APIRouter(prefix="/v1", tags=["usage"])

DEFAULT_RANGE_DAYS = 30


class UsageSummaryOut(BaseModel):
    message_count: int
    cost_usd: float


class UsageBreakdownOut(BaseModel):
    key: str
    message_count: int
    cost_usd: float


class UsageDailyPointOut(BaseModel):
    date: date
    message_count: int
    cost_usd: float


class UsageSummaryResponse(BaseModel):
    date_from: date
    date_to: date
    summary: UsageSummaryOut
    # Poblado solo cuando NO se filtra business_id: cuanto gasto cada negocio
    # de esta app en el rango. Null cuando ya se pidio uno puntual.
    by_business: list[UsageBreakdownOut] | None = None


class UsageDailyResponse(BaseModel):
    date_from: date
    date_to: date
    business_id: str | None
    channel: str | None
    days: list[UsageDailyPointOut]


class NotificationOut(BaseModel):
    id: str
    app_id: str
    business_id: str
    reference: str | None
    channel: str
    recipient: str
    status: str
    provider_message_id: str | None
    error: str | None
    cost_micros: int | None
    created_at: datetime


class NotificationListOut(BaseModel):
    notifications: list[NotificationOut]
    total: int
    limit: int
    offset: int


class PlatformUsageResponse(BaseModel):
    date_from: date
    date_to: date
    # Por app_id cuando no se filtra `app_id`; por business_id DENTRO de esa
    # app cuando si se filtra - mismo shape, distinta clave (ver `key`).
    breakdown: list[UsageBreakdownOut]


def _default_range(date_from: date | None, date_to: date | None) -> tuple[date, date]:
    """El rango por defecto, en UTC.

    `date.today()` da la fecha del SERVIDOR, y `notifications.created_at` se
    guarda en UTC (`datetime.utcnow`). En Colombia (UTC-5) eso significa que
    de 19:00 a medianoche la fecha local va un dia atras de la UTC: lo que se
    acaba de enviar queda FUERA del rango y el gasto del dia aparece en cero
    -- justo cuando cierra un local y alguien lo mira.

    Se resuelve del lado de la comparacion, no guardando fechas locales: el
    servicio sirve a apps en varios husos y la unica fecha que significa lo
    mismo para todas es la UTC.
    """
    end = date_to or datetime.now(UTC).date()
    start = date_from or (end - timedelta(days=DEFAULT_RANGE_DAYS - 1))
    return start, end


@router.get("/usage/summary", response_model=UsageSummaryResponse)
async def usage_summary(
    business_id: str | None = Query(default=None),
    channel: str | None = Query(default=None, description="Filtra a un canal, p.ej. 'whatsapp'."),
    date_from: date | None = Query(default=None),
    date_to: date | None = Query(default=None),
    app: AppIdentity = Depends(get_current_app),
    session: AsyncSession = Depends(get_session),
) -> UsageSummaryResponse:
    start, end = _default_range(date_from, date_to)
    service = UsageService(NotificationRepository(session))

    summary = await service.summary(app_id=app.app_id, business_id=business_id, channel=channel, date_from=start, date_to=end)

    by_business = None
    if business_id is None:
        rows = await service.by_business(app_id=app.app_id, date_from=start, date_to=end)
        by_business = [UsageBreakdownOut(key=r.key, **r.summary.__dict__) for r in rows]

    return UsageSummaryResponse(
        date_from=start,
        date_to=end,
        summary=UsageSummaryOut(**summary.__dict__),
        by_business=by_business,
    )


@router.get("/usage/daily", response_model=UsageDailyResponse)
async def usage_daily(
    business_id: str | None = Query(default=None),
    channel: str | None = Query(default=None),
    date_from: date | None = Query(default=None),
    date_to: date | None = Query(default=None),
    app: AppIdentity = Depends(get_current_app),
    session: AsyncSession = Depends(get_session),
) -> UsageDailyResponse:
    start, end = _default_range(date_from, date_to)
    service = UsageService(NotificationRepository(session))

    points = await service.daily_series(
        app_id=app.app_id, business_id=business_id, channel=channel, date_from=start, date_to=end
    )

    return UsageDailyResponse(
        date_from=start,
        date_to=end,
        business_id=business_id,
        channel=channel,
        days=[UsageDailyPointOut(date=p.date, **p.summary.__dict__) for p in points],
    )


@router.get("/platform/usage", response_model=PlatformUsageResponse, dependencies=[Depends(require_platform_access)])
async def platform_usage(
    app_id: str | None = Query(default=None, description="Filtra a una app: agrupa por negocio DENTRO de esa app en vez de por app."),
    date_from: date | None = Query(default=None),
    date_to: date | None = Query(default=None),
    session: AsyncSession = Depends(get_session),
) -> PlatformUsageResponse:
    start, end = _default_range(date_from, date_to)
    service = UsageService(NotificationRepository(session))

    rows = (
        await service.by_business(app_id=app_id, date_from=start, date_to=end)
        if app_id
        else await service.by_app(date_from=start, date_to=end)
    )

    return PlatformUsageResponse(
        date_from=start,
        date_to=end,
        breakdown=[UsageBreakdownOut(key=r.key, **r.summary.__dict__) for r in rows],
    )


@router.get(
    "/platform/notifications", response_model=NotificationListOut, dependencies=[Depends(require_platform_access)]
)
async def platform_notifications(
    app_id: str | None = Query(default=None),
    business_id: str | None = Query(default=None),
    channel: str | None = Query(default=None),
    status_filter: str | None = Query(default=None, alias="status"),
    reference: str | None = Query(default=None),
    date_from: date | None = Query(default=None),
    date_to: date | None = Query(default=None),
    limit: int = Query(default=50, ge=1, le=200),
    offset: int = Query(default=0, ge=0),
    session: AsyncSession = Depends(get_session),
) -> NotificationListOut:
    repository = NotificationRepository(session)
    rows, total = await repository.list_notifications(
        app_id=app_id,
        business_id=business_id,
        channel=channel,
        status=status_filter,
        reference=reference,
        date_from=date_from,
        date_to=date_to,
        limit=limit,
        offset=offset,
    )

    return NotificationListOut(
        notifications=[NotificationOut.model_validate(row, from_attributes=True) for row in rows],
        total=total,
        limit=limit,
        offset=offset,
    )
