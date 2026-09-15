"""Consulta y re-lanzamiento de eventos de webhook, para el panel Connect.

Autorizado por SCOPE (`get_panel_scope`): plataforma y la platform key ven
todo; un cliente externo ve unicamente los eventos de sus apps - el filtro
se aplica en el servidor, y pedir un evento ajeno responde 404 (no se le
confirma que exista). El payload crudo solo se devuelve en el detalle de
UN evento, no en el listado - los listados son para triage (que fallo,
cuando, por que), no para volcar mensajes de clientes en masa.
"""
from __future__ import annotations

from datetime import datetime

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from nexolu_comms_api.core.auth.dependencies import get_panel_scope, require_scope_for_app
from nexolu_comms_api.core.auth.panel import PanelScope
from nexolu_comms_api.core.db.entities import WebhookEvent
from nexolu_comms_api.core.db.session import get_session
from nexolu_comms_api.core.webhooks import forwarder

router = APIRouter(prefix="/v1/admin/webhook-events", tags=["admin"])


class WebhookEventOut(BaseModel):
    id: str
    app_id: str
    event_type: str
    phone_number_id: str | None
    signature_valid: bool | None
    forward_status: str
    attempts: int
    next_retry_at: datetime | None
    last_error: str | None
    received_at: datetime
    delivered_at: datetime | None


class WebhookEventDetailOut(WebhookEventOut):
    payload: str


class WebhookEventListOut(BaseModel):
    total: int
    items: list[WebhookEventOut]


def _to_out(event: WebhookEvent) -> WebhookEventOut:
    return WebhookEventOut(
        id=event.id,
        app_id=event.app_id,
        event_type=event.event_type,
        phone_number_id=event.phone_number_id,
        signature_valid=event.signature_valid,
        forward_status=event.forward_status,
        attempts=event.attempts,
        next_retry_at=event.next_retry_at,
        last_error=event.last_error,
        received_at=event.received_at,
        delivered_at=event.delivered_at,
    )


@router.get("", response_model=WebhookEventListOut)
async def list_events(
    app_id: str | None = None,
    forward_status: str | None = None,
    event_type: str | None = None,
    limit: int = Query(default=50, ge=1, le=200),
    offset: int = Query(default=0, ge=0),
    scope: PanelScope = Depends(get_panel_scope),
    session: AsyncSession = Depends(get_session),
) -> WebhookEventListOut:
    filters = []
    if scope.app_ids is not None:
        filters.append(WebhookEvent.app_id.in_(scope.app_ids))
    if app_id:
        filters.append(WebhookEvent.app_id == app_id)
    if forward_status:
        filters.append(WebhookEvent.forward_status == forward_status)
    if event_type:
        filters.append(WebhookEvent.event_type == event_type)

    total = (await session.execute(select(func.count()).select_from(WebhookEvent).where(*filters))).scalar_one()
    rows = (
        await session.execute(
            select(WebhookEvent).where(*filters).order_by(WebhookEvent.received_at.desc()).limit(limit).offset(offset)
        )
    ).scalars()

    return WebhookEventListOut(total=total, items=[_to_out(e) for e in rows])


@router.get("/{event_id}", response_model=WebhookEventDetailOut)
async def get_event(
    event_id: str,
    scope: PanelScope = Depends(get_panel_scope),
    session: AsyncSession = Depends(get_session),
) -> WebhookEventDetailOut:
    event = await session.get(WebhookEvent, event_id)
    if event is None:
        raise HTTPException(status_code=404, detail="Evento desconocido.")
    require_scope_for_app(scope, event.app_id)
    return WebhookEventDetailOut(**_to_out(event).model_dump(), payload=event.payload)


@router.post("/{event_id}/retry", response_model=WebhookEventOut)
async def retry_event(
    event_id: str,
    scope: PanelScope = Depends(get_panel_scope),
    session: AsyncSession = Depends(get_session),
) -> WebhookEventOut:
    """Re-lanza un evento a mano, sin esperar el backoff. Sirve para `dead`
    (la app ya volvio), `failed` (no esperar), y hasta `skipped` (la app ya
    configuro su callback). `delivered` se rechaza: reenviar un evento ya
    entregado es fabricar un duplicado; `rejected` tambien: su firma nunca
    fue valida."""
    event = await session.get(WebhookEvent, event_id)
    if event is None:
        raise HTTPException(status_code=404, detail="Evento desconocido.")
    require_scope_for_app(scope, event.app_id)
    if event.forward_status in (forwarder.STATUS_DELIVERED, forwarder.STATUS_REJECTED):
        raise HTTPException(status_code=409, detail=f"Un evento '{event.forward_status}' no se re-lanza.")

    # Volver a `pending` con reintentos frescos: un re-lanzamiento manual es
    # una decision nueva del operador, no la continuacion del backoff viejo.
    event.forward_status = forwarder.STATUS_PENDING
    event.attempts = 0
    event.next_retry_at = None
    await session.commit()

    await forwarder.attempt_forward(event.id)

    refreshed = await session.get(WebhookEvent, event.id)
    assert refreshed is not None
    await session.refresh(refreshed)
    return _to_out(refreshed)
