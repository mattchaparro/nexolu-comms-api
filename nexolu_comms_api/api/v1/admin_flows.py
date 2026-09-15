"""Gestion de flujos y contactos desde el panel Connect, por SCOPE (la
plataforma opera cualquier app; un cliente externo, solo las suyas).

La definicion de un flujo se valida AL GUARDAR (`validate_definition`): un
flujo roto rebota aca con el motivo exacto, nunca se descubre con un
cliente en vivo. Los contactos exponen tags/fields editables - el canal de
intercambio de datos entre flujos y apps.
"""
from __future__ import annotations

from datetime import datetime
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from nexolu_comms_api.core.auth.dependencies import get_panel_scope, require_scope_for_app
from nexolu_comms_api.core.auth.panel import PanelScope
from nexolu_comms_api.core.db.entities import Contact, Flow
from nexolu_comms_api.core.db.session import get_session
from nexolu_comms_api.core.flows.engine import (
    ContactRepository,
    FlowDefinitionError,
    validate_definition,
)

router = APIRouter(prefix="/v1/admin", tags=["admin"])


# --- flujos -------------------------------------------------------------------


class FlowOut(BaseModel):
    id: str
    app_id: str
    business_id: str
    name: str
    trigger_type: str
    trigger_keywords: list[str]
    is_active: bool
    definition: dict[str, Any]
    created_at: datetime
    updated_at: datetime


class FlowListOut(BaseModel):
    items: list[FlowOut]


class FlowIn(BaseModel):
    app_id: str = Field(min_length=1)
    business_id: str = Field(default="", description="Vacio = flujo a nivel de app.")
    name: str = Field(min_length=1, max_length=128, pattern=r"^[a-z0-9_\-]+$")
    trigger_type: str = Field(default="api", pattern="^(keyword|api)$")
    trigger_keywords: list[str] = Field(default_factory=list)
    is_active: bool = True
    definition: dict[str, Any]


class FlowPatch(BaseModel):
    trigger_type: str | None = Field(default=None, pattern="^(keyword|api)$")
    trigger_keywords: list[str] | None = None
    is_active: bool | None = None
    definition: dict[str, Any] | None = None


def _flow_out(flow: Flow) -> FlowOut:
    return FlowOut(
        id=flow.id,
        app_id=flow.app_id,
        business_id=flow.business_id,
        name=flow.name,
        trigger_type=flow.trigger_type,
        trigger_keywords=flow.trigger_keywords,
        is_active=flow.is_active,
        definition=flow.definition,
        created_at=flow.created_at,
        updated_at=flow.updated_at,
    )


def _validate(definition: dict[str, Any], trigger_type: str, keywords: list[str]) -> None:
    try:
        validate_definition(definition)
    except FlowDefinitionError as exc:
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail=str(exc)) from exc
    if trigger_type == "keyword" and not keywords:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="Un flujo con disparador 'keyword' necesita al menos una palabra clave.",
        )


@router.get("/flows", response_model=FlowListOut)
async def list_flows(
    app_id: str | None = None,
    scope: PanelScope = Depends(get_panel_scope),
    session: AsyncSession = Depends(get_session),
) -> FlowListOut:
    query = select(Flow).order_by(Flow.updated_at.desc())
    if app_id:
        query = query.where(Flow.app_id == app_id)
    rows = [f for f in (await session.execute(query)).scalars() if scope.allows(f.app_id)]
    return FlowListOut(items=[_flow_out(f) for f in rows])


@router.post("/flows", response_model=FlowOut, status_code=status.HTTP_201_CREATED)
async def create_flow(
    payload: FlowIn,
    scope: PanelScope = Depends(get_panel_scope),
    session: AsyncSession = Depends(get_session),
) -> FlowOut:
    require_scope_for_app(scope, payload.app_id)
    _validate(payload.definition, payload.trigger_type, payload.trigger_keywords)

    existing = (
        await session.execute(
            select(Flow).where(
                Flow.app_id == payload.app_id,
                Flow.business_id == payload.business_id,
                Flow.name == payload.name,
            )
        )
    ).scalar_one_or_none()
    if existing is not None:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=f"El flujo '{payload.name}' ya existe.")

    flow = Flow(
        app_id=payload.app_id,
        business_id=payload.business_id,
        name=payload.name,
        trigger_type=payload.trigger_type,
        trigger_keywords=payload.trigger_keywords,
        is_active=payload.is_active,
        definition=payload.definition,
    )
    session.add(flow)
    await session.commit()
    return _flow_out(flow)


@router.patch("/flows/{flow_id}", response_model=FlowOut)
async def update_flow(
    flow_id: str,
    payload: FlowPatch,
    scope: PanelScope = Depends(get_panel_scope),
    session: AsyncSession = Depends(get_session),
) -> FlowOut:
    flow = await session.get(Flow, flow_id)
    if flow is None:
        raise HTTPException(status_code=404, detail="Flujo desconocido.")
    require_scope_for_app(scope, flow.app_id)

    if payload.trigger_type is not None:
        flow.trigger_type = payload.trigger_type
    if payload.trigger_keywords is not None:
        flow.trigger_keywords = payload.trigger_keywords
    if payload.is_active is not None:
        flow.is_active = payload.is_active
    if payload.definition is not None:
        flow.definition = payload.definition
    _validate(flow.definition, flow.trigger_type, flow.trigger_keywords)

    await session.commit()
    return _flow_out(flow)


@router.delete("/flows/{flow_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_flow(
    flow_id: str,
    scope: PanelScope = Depends(get_panel_scope),
    session: AsyncSession = Depends(get_session),
) -> None:
    flow = await session.get(Flow, flow_id)
    if flow is None:
        raise HTTPException(status_code=404, detail="Flujo desconocido.")
    require_scope_for_app(scope, flow.app_id)
    await session.delete(flow)
    await session.commit()


# --- contactos ----------------------------------------------------------------


class ContactOut(BaseModel):
    id: str
    app_id: str
    business_id: str
    phone: str
    name: str
    tags: list[str]
    fields: dict[str, Any]
    last_inbound_at: datetime | None
    created_at: datetime


class ContactListOut(BaseModel):
    items: list[ContactOut]


class ContactPatch(BaseModel):
    name: str | None = None
    tags: list[str] | None = None
    fields: dict[str, Any] | None = None


def _contact_out(contact: Contact) -> ContactOut:
    return ContactOut(
        id=contact.id,
        app_id=contact.app_id,
        business_id=contact.business_id,
        phone=contact.phone,
        name=contact.name,
        tags=contact.tags,
        fields=contact.fields,
        last_inbound_at=contact.last_inbound_at,
        created_at=contact.created_at,
    )


@router.get("/contacts", response_model=ContactListOut)
async def list_contacts(
    app_id: str | None = None,
    scope: PanelScope = Depends(get_panel_scope),
    session: AsyncSession = Depends(get_session),
) -> ContactListOut:
    rows = await ContactRepository(session).list_contacts(app_id)
    rows = [c for c in rows if scope.allows(c.app_id)]
    return ContactListOut(items=[_contact_out(c) for c in rows])


@router.patch("/contacts/{contact_id}", response_model=ContactOut)
async def update_contact(
    contact_id: str,
    payload: ContactPatch,
    scope: PanelScope = Depends(get_panel_scope),
    session: AsyncSession = Depends(get_session),
) -> ContactOut:
    contact = await session.get(Contact, contact_id)
    if contact is None:
        raise HTTPException(status_code=404, detail="Contacto desconocido.")
    require_scope_for_app(scope, contact.app_id)

    if payload.name is not None:
        contact.name = payload.name
    if payload.tags is not None:
        contact.tags = list(dict.fromkeys(payload.tags))
    if payload.fields is not None:
        contact.fields = payload.fields
    await session.commit()
    return _contact_out(contact)
