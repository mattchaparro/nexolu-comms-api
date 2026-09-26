"""Cuánto se gasta en WhatsApp, según Meta.

Por qué no con nuestras cuentas. Connect estima un costo al enviar, pero
no sabe lo que Meta decide después: si el mensaje se entregó, si cayó en
la ventana gratis de atención, o si Meta lo recategorizó de utilidad a
marketing. La cifra que se paga es la de Meta, y Meta la da por API:
`pricing_analytics` de la cuenta de WhatsApp (WABA), en la moneda en que
esa cuenta factura (Luxury: COP).

Se cachea una hora: los datos de Meta van con retraso de horas y la
pantalla se abre muchas veces.
"""
from __future__ import annotations

import time
from datetime import UTC, datetime
from typing import Any

import httpx
from sqlalchemy.ext.asyncio import AsyncSession

from nexolu_comms_api.config import get_settings
from nexolu_comms_api.core.auth.apps import AppIdentity
from nexolu_comms_api.core.channels.business_channels import resolve_whatsapp_identity

CACHE_SECONDS = 3600
_cache: dict[tuple[str, str], tuple[float, dict[str, Any]]] = {}


class SpendError(Exception):
    """Meta no respondió o la cuenta no está configurada."""


def month_bounds(month: str | None) -> tuple[str, int, int]:
    """'2026-09' -> (mes, inicio, fin) en epoch UTC. Sin mes: el actual."""
    today = datetime.now(UTC).date()
    if month:
        year, mon = (int(x) for x in month.split("-"))
    else:
        year, mon = today.year, today.month
    start = datetime(year, mon, 1, tzinfo=UTC)
    end = datetime(year + (mon == 12), mon % 12 + 1, 1, tzinfo=UTC)
    now = datetime.now(UTC)
    return f"{year:04d}-{mon:02d}", int(start.timestamp()), int(min(end, now).timestamp())


async def whatsapp_spend(
    session: AsyncSession, app: AppIdentity, business_id: str | None, month: str | None
) -> dict[str, Any]:
    if app.whatsapp is None:
        raise SpendError("La app no tiene WhatsApp configurado.")

    identity, _ = await resolve_whatsapp_identity(session, app, business_id or app.app_id)
    wa = identity.whatsapp
    if wa is None or not wa.waba_id:
        raise SpendError("La cuenta de WhatsApp no tiene waba_id configurado.")

    label, start, end = month_bounds(month)
    key = (wa.waba_id, label)
    cached = _cache.get(key)
    if cached and time.time() - cached[0] < CACHE_SECONDS:
        return cached[1]

    settings = get_settings()
    field = (
        f"pricing_analytics.start({start}).end({end}).granularity(DAILY)"
        '.dimensions(["PRICING_CATEGORY","PRICING_TYPE"])'
    )
    try:
        async with httpx.AsyncClient(timeout=30) as client:
            response = await client.get(
                f"{settings.whatsapp_api_base_url}/{wa.waba_id}",
                params={"fields": f"currency,{field}"},
                headers={"Authorization": f"Bearer {wa.access_token}"},
            )
    except httpx.HTTPError as exc:
        raise SpendError(f"Meta no respondió: {exc}") from exc

    body = response.json()
    if "error" in body:
        raise SpendError(str(body["error"].get("message", body["error"])))

    currency = str(body.get("currency") or "USD")
    points = (body.get("pricing_analytics", {}).get("data") or [{}])[0].get("data_points", [])

    categories: dict[tuple[str, str], dict[str, Any]] = {}
    days: dict[str, dict[str, Any]] = {}
    for point in points:
        category = str(point.get("pricing_category") or "OTHER")
        kind = str(point.get("pricing_type") or "REGULAR")
        volume = int(point.get("volume") or 0)
        cost = float(point.get("cost") or 0)

        row = categories.setdefault((category, kind), {"category": category, "type": kind, "volume": 0, "cost": 0.0})
        row["volume"] += volume
        row["cost"] += cost

        day = datetime.fromtimestamp(int(point.get("start") or start), UTC).date().isoformat()
        d = days.setdefault(day, {"date": day, "volume": 0, "cost": 0.0})
        d["volume"] += volume
        d["cost"] += cost

    total = round(sum(r["cost"] for r in categories.values()), 2)
    rate = settings.usd_cop_rate
    result = {
        "month": label,
        "waba_id": wa.waba_id,
        "currency": currency,
        "total": total,
        # Dólares aproximados: la cifra exacta es la de la moneda de la cuenta.
        "usd_rate": rate if currency == "COP" else None,
        "total_usd": round(total / rate, 2) if currency == "COP" and rate else (total if currency == "USD" else None),
        "volume": sum(r["volume"] for r in categories.values()),
        "by_category": sorted(
            ({**r, "cost": round(r["cost"], 2)} for r in categories.values()), key=lambda r: -r["cost"]
        ),
        "daily": [{**d, "cost": round(d["cost"], 2)} for d in sorted(days.values(), key=lambda d: d["date"])],
        "updated_at": datetime.now(UTC).isoformat(),
    }
    _cache[key] = (time.time(), result)
    return result
