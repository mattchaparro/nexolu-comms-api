"""Sincronizacion de catalogo POR LA APP duena (auth de app, como el envio).

El circuito completo del brief: la app (POS) marca productos como
`available_on_whatsapp`, y en cada cambio manda aca el lote normalizado;
este servicio lo traduce al items_batch oficial, salta lo que no cambio
(content_hash) y deja el estado por item consultable. El webhook `order`
del carrito ya llega a la app por el reenvio normal - el pedido se crea
alla, no aca (principio 45).
"""
from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel, Field
from sqlalchemy.ext.asyncio import AsyncSession

from nexolu_comms_api.core.auth.apps import AppIdentity
from nexolu_comms_api.core.auth.dependencies import get_current_app
from nexolu_comms_api.core.catalog.service import (
    CatalogRepository,
    CatalogTargetError,
    check_pending,
    resolve_target,
    sync_items,
)
from nexolu_comms_api.core.db.session import get_session
from nexolu_comms_api.core.meta.graph import MetaGraphError

router = APIRouter(prefix="/v1/catalog", tags=["catalog"])


class CatalogItemIn(BaseModel):
    retailer_id: str = Field(min_length=1, max_length=191)
    title: str = Field(min_length=1, max_length=191)
    # Formato oficial: "<monto> <moneda>", p.ej. "9000 COP".
    price: str = Field(min_length=1, max_length=32)
    description: str | None = None
    availability: str = Field(default="in stock", pattern="^(in stock|out of stock)$")
    image_link: str | None = None
    link: str | None = None
    brand: str | None = None


class CatalogSyncIn(BaseModel):
    business_id: str | None = None
    items: list[CatalogItemIn] = Field(default_factory=list)
    # retailer_ids a borrar del catalogo de Meta (y del espejo).
    deletes: list[str] = Field(default_factory=list)


class CatalogSyncOut(BaseModel):
    sent: int
    skipped: int  # sin cambios desde el ultimo sync (content_hash)
    deleted: int
    handle: str | None
    immediate_errors: list[str]


class CatalogItemOut(BaseModel):
    retailer_id: str
    title: str
    price: str
    availability: str
    sync_status: str
    last_error: str | None
    last_synced_at: str | None


class CatalogStatusOut(BaseModel):
    catalog_id: str
    items: list[CatalogItemOut]


@router.post("/sync", response_model=CatalogSyncOut)
async def sync(
    payload: CatalogSyncIn,
    app: AppIdentity = Depends(get_current_app),
    session: AsyncSession = Depends(get_session),
) -> CatalogSyncOut:
    if not payload.items and not payload.deletes:
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail="Nada que sincronizar.")

    try:
        target = await resolve_target(session, app, payload.business_id)
    except CatalogTargetError as exc:
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail=str(exc)) from exc

    try:
        outcome = await sync_items(
            session,
            app,
            target,
            [item.model_dump() for item in payload.items],
            payload.deletes,
        )
    except MetaGraphError as exc:
        raise HTTPException(status_code=502, detail=exc.detail) from exc

    return CatalogSyncOut(
        sent=outcome.sent,
        skipped=outcome.skipped,
        deleted=outcome.deleted,
        handle=outcome.handle,
        immediate_errors=outcome.immediate_errors,
    )


@router.post("/check", response_model=dict)
async def check(
    payload: CatalogSyncIn,
    app: AppIdentity = Depends(get_current_app),
    session: AsyncSession = Depends(get_session),
) -> dict:
    """Resuelve los items `pending` contra check_batch_request_status."""
    try:
        target = await resolve_target(session, app, payload.business_id)
        return await check_pending(session, app, target)
    except CatalogTargetError as exc:
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail=str(exc)) from exc
    except MetaGraphError as exc:
        raise HTTPException(status_code=502, detail=exc.detail) from exc


@router.get("/status", response_model=CatalogStatusOut)
async def catalog_status(
    business_id: str | None = None,
    app: AppIdentity = Depends(get_current_app),
    session: AsyncSession = Depends(get_session),
) -> CatalogStatusOut:
    try:
        target = await resolve_target(session, app, business_id)
    except CatalogTargetError as exc:
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail=str(exc)) from exc

    rows = await CatalogRepository(session).list_items(app_id=app.app_id, catalog_id=target.catalog_id)
    return CatalogStatusOut(
        catalog_id=target.catalog_id,
        items=[
            CatalogItemOut(
                retailer_id=row.retailer_id,
                title=row.title,
                price=row.price,
                availability=row.availability,
                sync_status=row.sync_status,
                last_error=row.last_error,
                last_synced_at=row.last_synced_at.isoformat() if row.last_synced_at else None,
            )
            for row in rows
        ],
    )
