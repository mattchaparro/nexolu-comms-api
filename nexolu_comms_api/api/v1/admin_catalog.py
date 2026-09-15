"""Catalogo desde el panel Connect, por SCOPE: crear/conectar el catalogo
de una app o de un negocio, ver el estado de sync por item y re-verificar
lotes pendientes.

La condicion oficial que el panel debe hacer visible: los Terminos de
catalogo se aceptan creando el PRIMER catalogo del negocio via Business
Manager (paso manual unico del cliente); si nunca se hizo, el create por
API falla y el detalle del 502 lo dice.
"""
from __future__ import annotations

from datetime import datetime

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel, Field
from sqlalchemy.ext.asyncio import AsyncSession

from nexolu_comms_api.core.auth.apps import resolve_by_app_id
from nexolu_comms_api.core.auth.dependencies import get_panel_scope, require_scope_for_app
from nexolu_comms_api.core.auth.panel import PanelScope
from nexolu_comms_api.core.auth.repository import CommsAppRepository, ProviderCredentialRepository
from nexolu_comms_api.core.catalog.service import (
    CatalogRepository,
    CatalogTargetError,
    check_pending,
    resolve_target,
)
from nexolu_comms_api.core.channels.business_channels import BusinessChannelRepository
from nexolu_comms_api.core.db.session import get_session
from nexolu_comms_api.core.meta.graph import MetaGraphClient, MetaGraphError

router = APIRouter(prefix="/v1/admin", tags=["admin"])


class CatalogItemAdminOut(BaseModel):
    id: str
    app_id: str
    business_channel_id: str | None
    catalog_id: str
    retailer_id: str
    title: str
    price: str
    availability: str
    sync_status: str
    last_error: str | None
    last_synced_at: datetime | None
    updated_at: datetime


class CatalogItemListOut(BaseModel):
    items: list[CatalogItemAdminOut]


class CatalogSetupIn(BaseModel):
    app_id: str = Field(min_length=1)
    business_id: str | None = None
    # Conectar uno existente: solo catalog_id. Crear uno nuevo: name (+
    # meta_business_id si la credencial no lo tiene configurado).
    catalog_id: str | None = None
    name: str | None = None
    meta_business_id: str | None = None


class CatalogSetupOut(BaseModel):
    catalog_id: str
    connected_to_waba: bool


class CatalogCheckIn(BaseModel):
    app_id: str = Field(min_length=1)
    business_id: str | None = None


@router.get("/catalog-items", response_model=CatalogItemListOut)
async def list_catalog_items(
    app_id: str | None = None,
    scope: PanelScope = Depends(get_panel_scope),
    session: AsyncSession = Depends(get_session),
) -> CatalogItemListOut:
    rows = await CatalogRepository(session).list_items(app_id=app_id)
    rows = [row for row in rows if scope.allows(row.app_id)]
    return CatalogItemListOut(
        items=[
            CatalogItemAdminOut(
                id=row.id,
                app_id=row.app_id,
                business_channel_id=row.business_channel_id,
                catalog_id=row.catalog_id,
                retailer_id=row.retailer_id,
                title=row.title,
                price=row.price,
                availability=row.availability,
                sync_status=row.sync_status,
                last_error=row.last_error,
                last_synced_at=row.last_synced_at,
                updated_at=row.updated_at,
            )
            for row in rows
        ]
    )


@router.post("/catalog-items/check", response_model=dict)
async def check_items(
    payload: CatalogCheckIn,
    scope: PanelScope = Depends(get_panel_scope),
    session: AsyncSession = Depends(get_session),
) -> dict:
    require_scope_for_app(scope, payload.app_id)
    app = await resolve_by_app_id(session, payload.app_id)
    if app is None:
        raise HTTPException(status_code=404, detail="App desconocida.")
    try:
        target = await resolve_target(session, app, payload.business_id)
        return await check_pending(session, app, target)
    except CatalogTargetError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    except MetaGraphError as exc:
        raise HTTPException(status_code=502, detail=exc.detail) from exc


@router.post("/catalogs", response_model=CatalogSetupOut)
async def setup_catalog(
    payload: CatalogSetupIn,
    scope: PanelScope = Depends(get_panel_scope),
    session: AsyncSession = Depends(get_session),
) -> CatalogSetupOut:
    """Crea (o toma) un catalogo, lo conecta a la WABA del destino y guarda
    el catalog_id donde el envio y el sync lo resuelven: el canal del
    negocio, o la config de la credencial de la app."""
    require_scope_for_app(scope, payload.app_id)
    app = await resolve_by_app_id(session, payload.app_id)
    if app is None or app.whatsapp is None:
        raise HTTPException(status_code=404, detail="App desconocida o sin WhatsApp configurado.")

    graph = MetaGraphClient()

    # Destino: canal propio del negocio o credencial compartida de la app.
    channel = None
    if payload.business_id:
        channel = await BusinessChannelRepository(session).get_active_for_business(
            app.app_id, payload.business_id
        )
        if channel is None:
            raise HTTPException(
                status_code=422, detail=f"El negocio '{payload.business_id}' no tiene un numero propio activo."
            )
        token, waba_id = channel.access_token, channel.waba_id
    else:
        token, waba_id = app.whatsapp.access_token, app.whatsapp.waba_id
        if not waba_id:
            raise HTTPException(
                status_code=422, detail=f"La app '{payload.app_id}' no tiene waba_id configurado."
            )

    catalog_id = payload.catalog_id
    if not catalog_id:
        if not payload.name:
            raise HTTPException(
                status_code=422, detail="Para crear un catalogo hace falta 'name' (o pasa 'catalog_id' para conectar uno existente)."
            )
        meta_business_id = payload.meta_business_id or (
            app.whatsapp.meta_business_id if not payload.business_id else None
        )
        if not meta_business_id:
            raise HTTPException(
                status_code=422,
                detail="Para crear un catalogo hace falta 'meta_business_id' (el portafolio de Meta Business dueno de la WABA).",
            )
        try:
            catalog_id = await graph.create_catalog(meta_business_id, token, name=payload.name)
        except MetaGraphError as exc:
            raise HTTPException(status_code=502, detail=exc.detail) from exc

    try:
        await graph.connect_catalog_to_waba(waba_id, token, catalog_id)
    except MetaGraphError as exc:
        raise HTTPException(status_code=502, detail=exc.detail) from exc

    # Persistir donde la resolucion de identidad lo lee.
    if channel is not None:
        channel.catalog_id = catalog_id
    else:
        comms_app = await CommsAppRepository(session).get_by_app_id(app.app_id)
        if comms_app is None:
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail="La app resuelve por el registro legado (env): migrala a BD para guardar el catalog_id.",
            )
        credential = await ProviderCredentialRepository(session).get_active(comms_app.id, "meta_whatsapp")
        if credential is None:
            raise HTTPException(status_code=409, detail="La app no tiene credencial meta-whatsapp en BD.")
        credential.config = {**credential.config, "catalog_id": catalog_id}

    await session.commit()
    return CatalogSetupOut(catalog_id=catalog_id, connected_to_waba=True)

