"""Respuestas guardadas: lo que el negocio escribe veinte veces al dia.

Los precios, como llegar, los horarios. Se teclea "/precios" en la bandeja
y sale el texto completo.

Viven en Connect, no en cada app: la bandeja es una sola. Construir esto
tambien en el panel del Spa (y manana en el del POS) es justo lo que
vuelve imposible escalar -- cada mejora habria que hacerla dos veces y a
la tercera ya divergieron.
"""
from __future__ import annotations

from datetime import datetime

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from nexolu_comms_api.core.auth.dependencies import get_panel_scope, require_scope_for_app
from nexolu_comms_api.core.auth.panel import PanelScope
from nexolu_comms_api.core.db.entities import QuickReply
from nexolu_comms_api.core.db.session import get_session

router = APIRouter(prefix="/v1/admin/quick-replies", tags=["admin"])


class QuickReplyIn(BaseModel):
    app_id: str = Field(min_length=1)
    business_id: str = ""
    # Sin la barra y sin espacios: se escribe "/precios" y se guarda
    # "precios". Con espacios el atajo no se podria teclear de corrido.
    shortcut: str = Field(min_length=1, max_length=64, pattern=r"^[a-z0-9_-]+$")
    title: str = Field(default="", max_length=128)
    text: str = Field(min_length=1, max_length=4096)


class QuickReplyOut(QuickReplyIn):
    id: str
    created_at: datetime
    updated_at: datetime


def _to_out(row: QuickReply) -> QuickReplyOut:
    return QuickReplyOut(
        id=row.id,
        app_id=row.app_id,
        business_id=row.business_id,
        shortcut=row.shortcut,
        title=row.title,
        text=row.text,
        created_at=row.created_at,
        updated_at=row.updated_at,
    )


@router.get("", response_model=list[QuickReplyOut])
async def list_quick_replies(
    app_id: str | None = None,
    scope: PanelScope = Depends(get_panel_scope),
    session: AsyncSession = Depends(get_session),
) -> list[QuickReplyOut]:
    query = select(QuickReply).order_by(QuickReply.shortcut)
    if app_id:
        query = query.where(QuickReply.app_id == app_id)
    rows = (await session.execute(query)).scalars().all()
    return [_to_out(r) for r in rows if scope.allows(r.app_id)]


@router.put("", response_model=QuickReplyOut)
async def upsert_quick_reply(
    payload: QuickReplyIn,
    scope: PanelScope = Depends(get_panel_scope),
    session: AsyncSession = Depends(get_session),
) -> QuickReplyOut:
    """Upsert por atajo: guardar dos veces "/precios" es corregirlo, no
    terminar con dos respuestas distintas bajo el mismo atajo."""
    require_scope_for_app(scope, payload.app_id)

    row = (
        await session.execute(
            select(QuickReply).where(
                QuickReply.app_id == payload.app_id,
                QuickReply.business_id == payload.business_id,
                QuickReply.shortcut == payload.shortcut,
            )
        )
    ).scalars().first()

    if row is None:
        row = QuickReply(
            app_id=payload.app_id,
            business_id=payload.business_id,
            shortcut=payload.shortcut,
        )
        session.add(row)

    row.title = payload.title or payload.shortcut
    row.text = payload.text
    await session.commit()
    return _to_out(row)


@router.delete("/{quick_reply_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_quick_reply(
    quick_reply_id: str,
    scope: PanelScope = Depends(get_panel_scope),
    session: AsyncSession = Depends(get_session),
) -> None:
    row = await session.get(QuickReply, quick_reply_id)
    if row is None or not scope.allows(row.app_id):
        # 404 tambien para lo ajeno, como el resto del panel.
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="No existe esa respuesta.")

    await session.delete(row)
    await session.commit()
