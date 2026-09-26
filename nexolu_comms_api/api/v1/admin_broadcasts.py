"""Difusiones desde el panel: crear, programar, ver y cancelar.

Quien administra la app (get_panel_scope): una difusion le escribe a
cientos de clientas con el numero del negocio, y un error ahi baja la
calidad del numero para todos. La recepcionista de un salon no entra.

Las horas viajan en UTC; el panel las muestra en la hora del negocio.
"""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from nexolu_comms_api.core import broadcasts as core
from nexolu_comms_api.core.auth.dependencies import get_panel_scope
from nexolu_comms_api.core.auth.panel import PanelScope
from nexolu_comms_api.core.db.entities import Broadcast, BroadcastRecipient, Contact, WhatsAppTemplate
from nexolu_comms_api.core.db.session import get_session

router = APIRouter(prefix="/v1/admin/broadcasts", tags=["admin"])

EDITABLE = ("draft", "scheduled")


class Audience(BaseModel):
    last_visit_from: str | None = None
    last_visit_to: str | None = None
    no_visit_since: str | None = None
    include_never: bool = False
    visits_min: int | None = None
    visits_max: int | None = None
    attended_by: str | None = None
    tags_any: list[str] = Field(default_factory=list)
    tags_none: list[str] = Field(default_factory=list)
    contact_ids: list[str] = Field(default_factory=list)
    require_marketing_opt_in: bool = False


class BroadcastIn(BaseModel):
    app_id: str = Field(min_length=1)
    business_id: str = ""
    name: str = Field(min_length=1, max_length=128)
    template_name: str = Field(min_length=1, max_length=191)
    template_language: str = Field(default="es", max_length=16)
    # Una por variable del cuerpo; "{nombre}" se cambia por el nombre de
    # pila de cada clienta (vacio si no hay uno que sirva para saludar).
    template_params: list[str] = Field(default_factory=list)
    audience: Audience = Field(default_factory=Audience)
    # None = borrador. Con hora = programada (UTC, o con zona).
    scheduled_at: datetime | None = None


class BroadcastOut(BaseModel):
    id: str
    app_id: str
    business_id: str
    name: str
    template_name: str
    template_language: str
    template_params: list[str]
    audience: dict[str, Any]
    status: str
    scheduled_at: datetime | None
    sent_at: datetime | None
    recipients: int
    created_by: str | None
    created_at: datetime


class PreviewOut(BaseModel):
    count: int
    category: str | None
    template_status: str | None
    sample: list[dict[str, str]]


def _utc(value: datetime | None) -> datetime | None:
    if value is None or value.tzinfo is None:
        return value
    return value.astimezone(timezone.utc).replace(tzinfo=None)


def _to_out(row: Broadcast) -> BroadcastOut:
    return BroadcastOut(
        id=row.id,
        app_id=row.app_id,
        business_id=row.business_id,
        name=row.name,
        template_name=row.template_name,
        template_language=row.template_language,
        template_params=list(row.template_params or []),
        audience=dict(row.audience or {}),
        status=row.status,
        scheduled_at=row.scheduled_at,
        sent_at=row.sent_at,
        recipients=row.recipients,
        created_by=row.created_by,
        created_at=row.created_at,
    )


async def _get(session: AsyncSession, scope: PanelScope, broadcast_id: str) -> Broadcast:
    row = await session.get(Broadcast, broadcast_id)
    if row is None or not scope.allows(row.app_id):
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="No existe esa difusion.")
    return row


async def _check_template(session: AsyncSession, payload: BroadcastIn, scheduling: bool) -> None:
    template = (
        await session.execute(
            select(WhatsAppTemplate).where(
                WhatsAppTemplate.app_id == payload.app_id,
                WhatsAppTemplate.name == payload.template_name,
                WhatsAppTemplate.language == payload.template_language,
            )
        )
    ).scalars().first()
    if template is None:
        raise HTTPException(status_code=422, detail="Esa plantilla no existe en esta app.")
    # Programar con una plantilla sin aprobar es enterarse a la hora del
    # envio, mensaje por mensaje. Mejor ahora.
    if scheduling and template.status != "APPROVED":
        raise HTTPException(status_code=422, detail=f"La plantilla esta {template.status}: solo se programan aprobadas.")


def _apply(row: Broadcast, payload: BroadcastIn) -> None:
    row.business_id = payload.business_id
    row.name = payload.name.strip()
    row.template_name = payload.template_name
    row.template_language = payload.template_language
    row.template_params = payload.template_params
    row.audience = payload.audience.model_dump(exclude_defaults=True)
    row.scheduled_at = _utc(payload.scheduled_at)
    row.status = "scheduled" if payload.scheduled_at else "draft"


@router.get("", response_model=list[BroadcastOut])
async def list_broadcasts(
    app_id: str | None = None,
    scope: PanelScope = Depends(get_panel_scope),
    session: AsyncSession = Depends(get_session),
) -> list[BroadcastOut]:
    query = select(Broadcast).order_by(Broadcast.created_at.desc()).limit(200)
    if app_id:
        query = query.where(Broadcast.app_id == app_id)
    rows = (await session.execute(query)).scalars().all()
    return [_to_out(r) for r in rows if scope.allows(r.app_id)]


@router.post("/preview", response_model=PreviewOut)
async def preview(
    payload: BroadcastIn,
    scope: PanelScope = Depends(get_panel_scope),
    session: AsyncSession = Depends(get_session),
) -> PreviewOut:
    """A cuantas le llegaria HOY, y a quienes (muestra)."""
    if not scope.allows(payload.app_id):
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="App desconocida.")
    draft = Broadcast(
        app_id=payload.app_id,
        business_id=payload.business_id,
        template_name=payload.template_name,
        template_language=payload.template_language,
        audience=payload.audience.model_dump(exclude_defaults=True),
    )
    contacts = await core.audience_contacts(session, draft)
    template = (
        await session.execute(
            select(WhatsAppTemplate).where(
                WhatsAppTemplate.app_id == payload.app_id,
                WhatsAppTemplate.name == payload.template_name,
                WhatsAppTemplate.language == payload.template_language,
            )
        )
    ).scalars().first()
    return PreviewOut(
        count=len(contacts),
        category=template.category if template else None,
        template_status=template.status if template else None,
        sample=[{"name": c.name, "phone": c.phone} for c in contacts[:20]],
    )


@router.post("", response_model=BroadcastOut, status_code=status.HTTP_201_CREATED)
async def create_broadcast(
    payload: BroadcastIn,
    scope: PanelScope = Depends(get_panel_scope),
    session: AsyncSession = Depends(get_session),
) -> BroadcastOut:
    if not scope.allows(payload.app_id):
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="App desconocida.")
    await _check_template(session, payload, scheduling=payload.scheduled_at is not None)
    row = Broadcast(app_id=payload.app_id)
    _apply(row, payload)
    session.add(row)
    await session.commit()
    return _to_out(row)


@router.put("/{broadcast_id}", response_model=BroadcastOut)
async def update_broadcast(
    broadcast_id: str,
    payload: BroadcastIn,
    scope: PanelScope = Depends(get_panel_scope),
    session: AsyncSession = Depends(get_session),
) -> BroadcastOut:
    row = await _get(session, scope, broadcast_id)
    if row.status not in EDITABLE:
        raise HTTPException(status_code=409, detail="Ya salio: no se puede editar.")
    if payload.app_id != row.app_id:
        raise HTTPException(status_code=422, detail="No se puede cambiar de app.")
    await _check_template(session, payload, scheduling=payload.scheduled_at is not None)
    _apply(row, payload)
    await session.commit()
    return _to_out(row)


@router.post("/{broadcast_id}/cancel", response_model=BroadcastOut)
async def cancel_broadcast(
    broadcast_id: str,
    scope: PanelScope = Depends(get_panel_scope),
    session: AsyncSession = Depends(get_session),
) -> BroadcastOut:
    row = await _get(session, scope, broadcast_id)
    if row.status not in EDITABLE:
        raise HTTPException(status_code=409, detail="Ya salio: no se puede cancelar.")
    row.status = "cancelled"
    await session.commit()
    return _to_out(row)


@router.delete("/{broadcast_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_broadcast(
    broadcast_id: str,
    scope: PanelScope = Depends(get_panel_scope),
    session: AsyncSession = Depends(get_session),
) -> None:
    """Solo borradores y canceladas: lo que salio queda, es el registro de
    a quien se le escribio."""
    row = await _get(session, scope, broadcast_id)
    if row.status not in ("draft", "cancelled"):
        raise HTTPException(status_code=409, detail="Solo se borran borradores o canceladas.")
    await session.delete(row)
    await session.commit()


@router.get("/{broadcast_id}/report")
async def broadcast_report(
    broadcast_id: str,
    scope: PanelScope = Depends(get_panel_scope),
    session: AsyncSession = Depends(get_session),
) -> dict[str, Any]:
    row = await _get(session, scope, broadcast_id)
    return {"broadcast": _to_out(row).model_dump(mode="json"), **(await core.report(session, row))}


@router.get("/{broadcast_id}/recipients")
async def broadcast_recipients(
    broadcast_id: str,
    scope: PanelScope = Depends(get_panel_scope),
    session: AsyncSession = Depends(get_session),
) -> list[dict[str, Any]]:
    row = await _get(session, scope, broadcast_id)
    rows = (
        await session.execute(
            select(BroadcastRecipient, Contact.name)
            .join(Contact, Contact.id == BroadcastRecipient.contact_id, isouter=True)
            .where(BroadcastRecipient.broadcast_id == row.id)
        )
    ).all()
    return [
        {
            "contact_id": r.contact_id,
            "name": name or "",
            "phone": r.phone,
            "status": r.status,
            "error": r.error,
            "sent_at": r.sent_at.isoformat() if r.sent_at else None,
        }
        for r, name in rows
    ]
