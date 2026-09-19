"""Preferencias de avisos de la bandeja, por app/negocio.

Por que es configuracion y no una constante en el codigo: quien recibe los
avisos cambia (entra una recepcionista, el duenio se va de viaje) y cada
negocio aguanta un silencio distinto -- un spa con cita cada hora no es una
tienda que responde en 5 minutos. Una regla en el codigo obliga a un deploy
para algo que el negocio deberia cambiar solo.

Lo que NO se configura aca: los avisos de NEGOCIO (agendo, cancelo). Esos
los manda la app duena, que es la que sabe lo que paso (principio 45).
"""
from __future__ import annotations

from datetime import datetime

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from nexolu_comms_api.core.alerts import compose, pending_conversations
from nexolu_comms_api.core.auth.dependencies import get_panel_scope, require_scope_for_app
from nexolu_comms_api.core.auth.panel import PanelScope
from nexolu_comms_api.core.db.entities import InboxAlertConfig
from nexolu_comms_api.core.db.session import get_session

router = APIRouter(prefix="/v1/admin/inbox-alerts", tags=["admin"])


class AlertConfigIn(BaseModel):
    app_id: str = Field(min_length=1)
    business_id: str = ""
    is_active: bool = True
    emails: list[str] = Field(default_factory=list, max_length=10)
    whatsapp_to: str = ""
    # Plantilla para el aviso urgente cuando la ventana de 24h esta cerrada.
    # Vacia = fuera de ventana no se manda WhatsApp (el correo va igual).
    urgent_template: str = ""
    urgent_template_language: str = "es"
    quiet_minutes: int = Field(default=10, ge=1, le=1440)


class AlertConfigOut(AlertConfigIn):
    id: str
    created_at: datetime
    updated_at: datetime


class AlertPreviewOut(BaseModel):
    """Lo que se mandaria AHORA con esta configuracion. Sirve para probar
    sin esperar al worker ni molestar a nadie."""

    pending: int
    subject: str | None = None
    body: str | None = None


def _to_out(row: InboxAlertConfig) -> AlertConfigOut:
    return AlertConfigOut(
        id=row.id,
        app_id=row.app_id,
        business_id=row.business_id,
        is_active=row.is_active,
        emails=list(row.emails),
        whatsapp_to=row.whatsapp_to,
        urgent_template=row.urgent_template,
        urgent_template_language=row.urgent_template_language,
        quiet_minutes=row.quiet_minutes,
        created_at=row.created_at,
        updated_at=row.updated_at,
    )


@router.get("", response_model=list[AlertConfigOut])
async def list_configs(
    scope: PanelScope = Depends(get_panel_scope),
    session: AsyncSession = Depends(get_session),
) -> list[AlertConfigOut]:
    rows = (await session.execute(select(InboxAlertConfig))).scalars().all()
    return [_to_out(r) for r in rows if scope.allows(r.app_id)]


@router.put("", response_model=AlertConfigOut)
async def upsert_config(
    payload: AlertConfigIn,
    scope: PanelScope = Depends(get_panel_scope),
    session: AsyncSession = Depends(get_session),
) -> AlertConfigOut:
    """Upsert por (app, negocio): la configuracion es una sola por bandeja,
    no una lista de reglas que se pisan entre si."""
    require_scope_for_app(scope, payload.app_id)

    row = (
        await session.execute(
            select(InboxAlertConfig).where(
                InboxAlertConfig.app_id == payload.app_id,
                InboxAlertConfig.business_id == payload.business_id,
            )
        )
    ).scalars().first()

    if row is None:
        row = InboxAlertConfig(app_id=payload.app_id, business_id=payload.business_id)
        session.add(row)

    row.is_active = payload.is_active
    row.emails = [e.strip() for e in payload.emails if e.strip()]
    row.whatsapp_to = payload.whatsapp_to.strip().lstrip("+")
    row.urgent_template = payload.urgent_template.strip()
    row.urgent_template_language = payload.urgent_template_language
    row.quiet_minutes = payload.quiet_minutes
    await session.commit()

    return _to_out(row)


@router.get("/preview", response_model=AlertPreviewOut)
async def preview(
    app_id: str,
    business_id: str = "",
    scope: PanelScope = Depends(get_panel_scope),
    session: AsyncSession = Depends(get_session),
) -> AlertPreviewOut:
    require_scope_for_app(scope, app_id)

    row = (
        await session.execute(
            select(InboxAlertConfig).where(
                InboxAlertConfig.app_id == app_id,
                InboxAlertConfig.business_id == business_id,
            )
        )
    ).scalars().first()
    if row is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="Esta bandeja no tiene avisos configurados."
        )

    pending = await pending_conversations(session, row)
    if not pending:
        return AlertPreviewOut(pending=0)

    subject, body = compose(pending)
    return AlertPreviewOut(pending=len(pending), subject=subject, body=body)
