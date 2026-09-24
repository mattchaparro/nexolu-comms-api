"""Gestion de plantillas de WhatsApp desde el panel Connect.

Autorizado por SCOPE (`get_panel_scope`), como canales y credenciales: la
plataforma opera cualquier app; un cliente externo, SOLO las suyas (las
plantillas son de SU WABA - crearlas es parte de usar Connect). Lo ajeno
responde 404.

El estado que se muestra es el del espejo local (`whatsapp_templates`);
"Sincronizar" lo reconcilia contra Meta, y el webhook
`message_template_status_update` lo mantiene al dia solo (ver
core/templates/service.py).
"""
from __future__ import annotations

from datetime import datetime
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from nexolu_comms_api.core.auth.apps import AppIdentity, resolve_by_app_id
from nexolu_comms_api.core.auth.dependencies import (
    get_chat_scope,
    get_panel_scope,
    require_scope_for_app,
)
from nexolu_comms_api.core.auth.panel import PanelScope
from nexolu_comms_api.core.db.entities import BusinessChannel, WhatsAppTemplate
from nexolu_comms_api.core.db.session import get_session
from nexolu_comms_api.core.meta.graph import MetaGraphClient, MetaGraphError
from nexolu_comms_api.core.templates.service import (
    TemplateRepository,
    TemplateTarget,
    TemplateTargetError,
    resolve_target,
    sync_templates,
)

router = APIRouter(prefix="/v1/admin/templates", tags=["admin"])


class TemplateOut(BaseModel):
    id: str
    app_id: str
    business_channel_id: str | None
    waba_id: str
    name: str
    language: str
    category: str
    status: str
    meta_template_id: str | None
    components: list[dict[str, Any]]
    quality_score: str | None
    reason: str | None
    last_synced_at: datetime | None
    created_at: datetime


class TemplateListOut(BaseModel):
    items: list[TemplateOut]


class TemplateCreateIn(BaseModel):
    app_id: str = Field(min_length=1)
    # Con business_id, la plantilla se crea en la WABA PROPIA de ese
    # negocio (Embedded Signup); sin el, en la WABA compartida de la app.
    business_id: str | None = None
    # Reglas de Meta: minusculas, numeros y guion bajo.
    name: str = Field(min_length=1, max_length=191, pattern=r"^[a-z0-9_]+$")
    language: str = Field(default="es", min_length=2, max_length=16)
    category: str = Field(pattern="^(MARKETING|UTILITY|AUTHENTICATION)$")
    components: list[dict[str, Any]] = Field(min_length=1)


class TemplateSyncIn(BaseModel):
    app_id: str = Field(min_length=1)
    business_id: str | None = None


def _to_out(row: WhatsAppTemplate) -> TemplateOut:
    return TemplateOut(
        id=row.id,
        app_id=row.app_id,
        business_channel_id=row.business_channel_id,
        waba_id=row.waba_id,
        name=row.name,
        language=row.language,
        category=row.category,
        status=row.status,
        meta_template_id=row.meta_template_id,
        components=row.components,
        quality_score=row.quality_score,
        reason=row.reason,
        last_synced_at=row.last_synced_at,
        created_at=row.created_at,
    )


async def _app_and_target(
    session: AsyncSession, scope: PanelScope, app_id: str, business_id: str | None
) -> tuple[AppIdentity, TemplateTarget]:
    require_scope_for_app(scope, app_id)
    app = await resolve_by_app_id(session, app_id)
    if app is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="App desconocida.")
    try:
        target = await resolve_target(session, app, business_id)
    except TemplateTargetError as exc:
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail=str(exc)) from exc
    return app, target


@router.get("", response_model=TemplateListOut)
async def list_templates(
    app_id: str | None = None,
    scope: PanelScope = Depends(get_chat_scope),
    session: AsyncSession = Depends(get_session),
) -> TemplateListOut:
    # Lectura abierta a quien atiende el chat: fuera de las 24 h la unica
    # forma de escribirle a alguien es una plantilla. Crear, sincronizar y
    # borrar siguen siendo de quien administra la app entera.
    rows = await TemplateRepository(session).list_templates(app_id)
    rows = [row for row in rows if scope.allows(row.app_id)]
    if scope.is_business_restricted:
        # Las de la WABA compartida (sin canal) son de todos; las del numero
        # propio de un negocio, solo de ese negocio.
        channels = dict(
            (await session.execute(select(BusinessChannel.id, BusinessChannel.business_id))).all()
        )
        rows = [
            row
            for row in rows
            if row.business_channel_id is None
            or scope.allows_business(channels.get(row.business_channel_id))
        ]
    return TemplateListOut(items=[_to_out(row) for row in rows])


@router.post("", response_model=TemplateOut, status_code=status.HTTP_201_CREATED)
async def create_template(
    payload: TemplateCreateIn,
    scope: PanelScope = Depends(get_panel_scope),
    session: AsyncSession = Depends(get_session),
) -> TemplateOut:
    app, target = await _app_and_target(session, scope, payload.app_id, payload.business_id)
    repo = TemplateRepository(session)

    if await repo.get_by_identity(target.waba_id, payload.name, payload.language) is not None:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=f"'{payload.name}' ({payload.language}) ya existe en esa WABA.",
        )

    try:
        meta_response = await MetaGraphClient().create_template(
            target.waba_id,
            target.access_token,
            name=payload.name,
            language=payload.language,
            category=payload.category,
            components=payload.components,
        )
    except MetaGraphError as exc:
        raise HTTPException(status_code=502, detail=exc.detail) from exc

    row = await repo.upsert_from_meta(
        app_id=app.app_id,
        target=target,
        name=payload.name,
        language=payload.language,
        payload={
            "category": payload.category,
            "components": payload.components,
            # La respuesta de creacion trae id/status/category definitivos.
            **{k: v for k, v in meta_response.items() if k in ("id", "status", "category")},
        },
    )
    await session.commit()
    return _to_out(row)


@router.post("/sync", response_model=TemplateListOut)
async def sync(
    payload: TemplateSyncIn,
    scope: PanelScope = Depends(get_panel_scope),
    session: AsyncSession = Depends(get_session),
) -> TemplateListOut:
    app, target = await _app_and_target(session, scope, payload.app_id, payload.business_id)
    try:
        rows = await sync_templates(session, app, target)
    except MetaGraphError as exc:
        raise HTTPException(status_code=502, detail=exc.detail) from exc
    return TemplateListOut(items=[_to_out(row) for row in rows])


@router.delete("/{template_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_template(
    template_id: str,
    scope: PanelScope = Depends(get_panel_scope),
    session: AsyncSession = Depends(get_session),
) -> None:
    """Borra la plantilla en Meta y su espejo. OJO: Meta borra `name` en
    TODOS sus idiomas de esa WABA - por eso aca tambien se limpian las
    filas hermanas del mismo nombre."""
    repo = TemplateRepository(session)
    row = await repo.get(template_id)
    if row is None:
        raise HTTPException(status_code=404, detail="Plantilla desconocida.")
    require_scope_for_app(scope, row.app_id)

    app = await resolve_by_app_id(session, row.app_id)
    if app is None:
        raise HTTPException(status_code=404, detail="App desconocida.")

    # El token correcto es el del dueno de la WABA de la fila.
    try:
        if row.business_channel_id:
            from nexolu_comms_api.core.db.entities import BusinessChannel

            channel = await session.get(BusinessChannel, row.business_channel_id)
            if channel is None:
                raise HTTPException(status_code=409, detail="El canal de esta plantilla ya no existe.")
            token = channel.access_token
        else:
            target = await resolve_target(session, app, None)
            token = target.access_token
        await MetaGraphClient().delete_template(row.waba_id, token, row.name)
    except TemplateTargetError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    except MetaGraphError as exc:
        raise HTTPException(status_code=502, detail=exc.detail) from exc

    siblings = await repo.list_templates(app_id=row.app_id)
    for sibling in siblings:
        if sibling.waba_id == row.waba_id and sibling.name == row.name:
            await session.delete(sibling)
    await session.commit()
