"""Formularios de WhatsApp (WhatsApp Flows de Meta) desde el panel Connect.

En el panel se llaman "Formularios" para no confundirlos con los flujos
de Connect (motor tipo ManyChat, /v1/admin/flows). Autorizado por SCOPE,
igual que las plantillas: la plataforma opera cualquier app; un cliente
externo, solo las suyas - lo ajeno responde 404.

Ciclo (ver core/whatsapp_flows/service.py): crear borrador -> subir JSON
(validador local primero, despues el de Meta) -> vista previa -> publicar.
Uno publicado ya no se edita: se depreca y se crea otro.
"""
from __future__ import annotations

from datetime import datetime
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel, Field
from sqlalchemy.ext.asyncio import AsyncSession

from nexolu_comms_api.core.auth.apps import AppIdentity, resolve_by_app_id
from nexolu_comms_api.core.auth.dependencies import get_panel_scope, require_scope_for_app
from nexolu_comms_api.core.auth.panel import PanelScope
from nexolu_comms_api.core.db.entities import WhatsAppFlow
from nexolu_comms_api.core.db.session import get_session
from nexolu_comms_api.core.meta.graph import MetaGraphError
from nexolu_comms_api.core.templates.service import (
    TemplateTarget,
    TemplateTargetError,
    resolve_target,
)
from nexolu_comms_api.core.whatsapp_flows import service
from nexolu_comms_api.core.whatsapp_flows.generator import (
    FlowGenerationUnavailable,
    generate_flow_json,
)
from nexolu_comms_api.core.whatsapp_flows.library import LIBRARY, fresh_json, get_entry
from nexolu_comms_api.core.whatsapp_flows.provisioning import provision_from_library
from nexolu_comms_api.core.whatsapp_flows.validator import FlowIssue, validate_flow_json

router = APIRouter(prefix="/v1/admin/whatsapp-flows", tags=["admin"])


class FlowIssueOut(BaseModel):
    message: str
    path: str
    severity: str  # error | warning
    source: str  # local | meta
    line: int | None = None


class WhatsAppFlowOut(BaseModel):
    id: str
    app_id: str
    business_id: str | None
    business_channel_id: str | None
    waba_id: str
    meta_flow_id: str | None
    name: str
    categories: list[str]
    status: str
    flow_json: dict[str, Any] | None
    json_version: str | None
    validation_errors: list[FlowIssueOut]
    preview_url: str | None
    preview_expires_at: str | None
    library_key: str | None
    last_synced_at: datetime | None
    published_at: datetime | None
    created_at: datetime


class WhatsAppFlowListOut(BaseModel):
    items: list[WhatsAppFlowOut]


class TargetIn(BaseModel):
    app_id: str = Field(min_length=1)
    # Con business_id, en la WABA PROPIA de ese negocio (Embedded Signup);
    # sin el, en la WABA compartida de la app.
    business_id: str | None = None


class FlowCreateIn(TargetIn):
    name: str = Field(min_length=1, max_length=191)
    categories: list[str] = Field(min_length=1)
    flow_json: dict[str, Any]


class FlowJsonIn(BaseModel):
    flow_json: dict[str, Any]


class FlowValidateIn(BaseModel):
    flow_json: Any


class FlowValidateOut(BaseModel):
    issues: list[FlowIssueOut]


class FlowFromLibraryIn(TargetIn):
    key: str = Field(min_length=1)
    publish: bool = False


class FlowGenerateIn(TargetIn):
    name: str = Field(min_length=1, max_length=191)
    categories: list[str] = Field(min_length=1)
    description: str = Field(min_length=10, max_length=4000)


class FlowRegenerateIn(BaseModel):
    description: str = Field(min_length=10, max_length=4000)


class FlowGenerateOut(BaseModel):
    # El borrador en Meta (None si ningun intento paso el validador local:
    # entonces no se creo nada en Meta y el JSON queda para corregir a mano).
    flow: WhatsAppFlowOut | None
    flow_json: dict[str, Any] | None
    issues: list[FlowIssueOut]
    attempts: int
    ok: bool


class LibraryEntryOut(BaseModel):
    key: str
    name: str
    title: str
    description: str
    categories: list[str]
    flow_json: dict[str, Any]


class LibraryOut(BaseModel):
    items: list[LibraryEntryOut]


def _to_out(row: WhatsAppFlow) -> WhatsAppFlowOut:
    return WhatsAppFlowOut(
        id=row.id,
        app_id=row.app_id,
        business_id=row.business_id,
        business_channel_id=row.business_channel_id,
        waba_id=row.waba_id,
        meta_flow_id=row.meta_flow_id,
        name=row.name,
        categories=row.categories or [],
        status=row.status,
        flow_json=service.parse_stored(row),
        json_version=row.json_version,
        validation_errors=[FlowIssueOut(**_issue_fields(issue)) for issue in row.validation_errors or []],
        preview_url=row.preview_url,
        preview_expires_at=row.preview_expires_at,
        library_key=row.library_key,
        last_synced_at=row.last_synced_at,
        published_at=row.published_at,
        created_at=row.created_at,
    )


def _issue_fields(issue: dict[str, Any]) -> dict[str, Any]:
    return {
        "message": str(issue.get("message", "")),
        "path": str(issue.get("path", "")),
        "severity": str(issue.get("severity", "error")),
        "source": str(issue.get("source", "local")),
        "line": issue.get("line") if isinstance(issue.get("line"), int) else None,
    }


def _issues_out(issues: list[FlowIssue]) -> list[FlowIssueOut]:
    return [FlowIssueOut(**issue.to_dict()) for issue in issues]


def _check_categories(categories: list[str]) -> list[str]:
    unknown = [c for c in categories if c not in service.CATEGORIES]
    if unknown:
        raise HTTPException(
            status_code=422,
            detail=f"Categoria desconocida: {', '.join(unknown)}. Validas: {', '.join(service.CATEGORIES)}.",
        )
    return categories


def _validation_http_error(exc: service.FlowValidationError) -> HTTPException:
    """422 con los errores estructurados: el panel los pinta en su lista."""
    return HTTPException(
        status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
        detail={
            "message": "El formulario tiene errores (validador local): no se envio a Meta.",
            "issues": [issue.to_dict() for issue in exc.issues],
        },
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


async def _owned_row(session: AsyncSession, scope: PanelScope, flow_id: str) -> WhatsAppFlow:
    row = await service.FlowRepository(session).get(flow_id)
    if row is None:
        raise HTTPException(status_code=404, detail="Formulario desconocido.")
    require_scope_for_app(scope, row.app_id)
    return row


async def _ensure_name_free(session: AsyncSession, waba_id: str, name: str) -> None:
    if await service.FlowRepository(session).get_by_name(waba_id, name) is not None:
        raise HTTPException(status_code=409, detail=f"Ya hay un formulario '{name}' en esa WABA.")


async def _run(session: AsyncSession, operation) -> Any:
    """Ejecuta una operacion contra Meta con el mapeo de errores comun y
    hace commit si salio bien."""
    try:
        result = await operation
    except service.FlowValidationError as exc:
        raise _validation_http_error(exc) from exc
    except service.FlowStateError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except TemplateTargetError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    except MetaGraphError as exc:
        # Lo que ya se hubiera creado en Meta (un borrador) queda en el
        # espejo: sin el commit, reintentar chocaria con el nombre.
        await session.commit()
        raise HTTPException(status_code=502, detail=exc.detail) from exc
    await session.commit()
    return result


# -- lectura -----------------------------------------------------------------


@router.get("", response_model=WhatsAppFlowListOut)
async def list_flows(
    app_id: str | None = None,
    scope: PanelScope = Depends(get_panel_scope),
    session: AsyncSession = Depends(get_session),
) -> WhatsAppFlowListOut:
    rows = await service.FlowRepository(session).list_flows(app_id)
    return WhatsAppFlowListOut(items=[_to_out(row) for row in rows if scope.allows(row.app_id)])


@router.get("/library", response_model=LibraryOut)
async def library(_: PanelScope = Depends(get_panel_scope)) -> LibraryOut:
    return LibraryOut(
        items=[
            LibraryEntryOut(
                key=entry.key,
                name=entry.name,
                title=entry.title,
                description=entry.description,
                categories=list(entry.categories),
                flow_json=fresh_json(entry),
            )
            for entry in LIBRARY.values()
        ]
    )


@router.post("/validate", response_model=FlowValidateOut)
async def validate_locally(
    payload: FlowValidateIn, _: PanelScope = Depends(get_panel_scope)
) -> FlowValidateOut:
    """Solo el validador local (sin Meta): para el editor mientras se escribe."""
    return FlowValidateOut(issues=_issues_out(validate_flow_json(payload.flow_json)))


@router.get("/{flow_id}", response_model=WhatsAppFlowOut)
async def get_flow(
    flow_id: str,
    scope: PanelScope = Depends(get_panel_scope),
    session: AsyncSession = Depends(get_session),
) -> WhatsAppFlowOut:
    return _to_out(await _owned_row(session, scope, flow_id))


# -- ciclo de vida -------------------------------------------------------------


@router.post("", response_model=WhatsAppFlowOut, status_code=status.HTTP_201_CREATED)
async def create_flow(
    payload: FlowCreateIn,
    scope: PanelScope = Depends(get_panel_scope),
    session: AsyncSession = Depends(get_session),
) -> WhatsAppFlowOut:
    app, target = await _app_and_target(session, scope, payload.app_id, payload.business_id)
    await _ensure_name_free(session, target.waba_id, payload.name)
    row = await _run(
        session,
        service.create_draft(
            session,
            app=app,
            target=target,
            business_id=payload.business_id,
            name=payload.name,
            categories=_check_categories(payload.categories),
            flow_json=payload.flow_json,
        ),
    )
    return _to_out(row)


@router.put("/{flow_id}/json", response_model=WhatsAppFlowOut)
async def update_json(
    flow_id: str,
    payload: FlowJsonIn,
    scope: PanelScope = Depends(get_panel_scope),
    session: AsyncSession = Depends(get_session),
) -> WhatsAppFlowOut:
    row = await _owned_row(session, scope, flow_id)
    return _to_out(await _run(session, service.update_json(session, row, payload.flow_json)))


@router.post("/{flow_id}/refresh", response_model=WhatsAppFlowOut)
async def refresh(
    flow_id: str,
    scope: PanelScope = Depends(get_panel_scope),
    session: AsyncSession = Depends(get_session),
) -> WhatsAppFlowOut:
    """Relee de Meta estado, validation_errors y la URL de vista previa."""
    row = await _owned_row(session, scope, flow_id)
    return _to_out(await _run(session, service.refresh(session, row)))


@router.post("/{flow_id}/publish", response_model=WhatsAppFlowOut)
async def publish(
    flow_id: str,
    scope: PanelScope = Depends(get_panel_scope),
    session: AsyncSession = Depends(get_session),
) -> WhatsAppFlowOut:
    row = await _owned_row(session, scope, flow_id)
    return _to_out(await _run(session, service.publish(session, row)))


@router.post("/{flow_id}/deprecate", response_model=WhatsAppFlowOut)
async def deprecate(
    flow_id: str,
    scope: PanelScope = Depends(get_panel_scope),
    session: AsyncSession = Depends(get_session),
) -> WhatsAppFlowOut:
    row = await _owned_row(session, scope, flow_id)
    return _to_out(await _run(session, service.deprecate(session, row)))


@router.delete("/{flow_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_flow(
    flow_id: str,
    scope: PanelScope = Depends(get_panel_scope),
    session: AsyncSession = Depends(get_session),
) -> None:
    row = await _owned_row(session, scope, flow_id)
    await _run(session, service.delete_draft(session, row))


@router.post("/sync", response_model=WhatsAppFlowListOut)
async def sync(
    payload: TargetIn,
    scope: PanelScope = Depends(get_panel_scope),
    session: AsyncSession = Depends(get_session),
) -> WhatsAppFlowListOut:
    app, target = await _app_and_target(session, scope, payload.app_id, payload.business_id)
    rows = await _run(session, service.sync_flows(session, app, target, payload.business_id))
    return WhatsAppFlowListOut(items=[_to_out(row) for row in rows])


@router.post("/from-library", response_model=WhatsAppFlowOut, status_code=status.HTTP_201_CREATED)
async def create_from_library(
    payload: FlowFromLibraryIn,
    scope: PanelScope = Depends(get_panel_scope),
    session: AsyncSession = Depends(get_session),
) -> WhatsAppFlowOut:
    entry = get_entry(payload.key)
    if entry is None:
        raise HTTPException(status_code=404, detail=f"No hay plantilla '{payload.key}'.")
    app, target = await _app_and_target(session, scope, payload.app_id, payload.business_id)
    row = await _run(
        session,
        provision_from_library(
            session,
            app=app,
            target=target,
            business_id=payload.business_id,
            entry=entry,
            publish=payload.publish,
        ),
    )
    return _to_out(row)


# -- generacion con IA -----------------------------------------------------------


@router.post("/generate", response_model=FlowGenerateOut)
async def generate(
    payload: FlowGenerateIn,
    scope: PanelScope = Depends(get_panel_scope),
    session: AsyncSession = Depends(get_session),
) -> FlowGenerateOut:
    """Genera el JSON y lo valida contra Meta sobre un borrador NUEVO, que se
    crea recien con el primer intento que pasa el validador local."""
    app, target = await _app_and_target(session, scope, payload.app_id, payload.business_id)
    categories = _check_categories(payload.categories)
    await _ensure_name_free(session, target.waba_id, payload.name)

    draft: list[WhatsAppFlow] = []

    async def validate_remote(flow_json: dict[str, Any]) -> list[FlowIssue]:
        if not draft:
            row = await service.create_draft(
                session,
                app=app,
                target=target,
                business_id=payload.business_id,
                name=payload.name,
                categories=categories,
                flow_json=flow_json,
            )
            await session.commit()
            draft.append(row)
            return [FlowIssue(**_issue_fields(i)) for i in row.validation_errors if i.get("source") == "meta"]
        issues = await service.upload_for_validation(session, draft[0], flow_json)
        await session.commit()
        return issues

    return await _generate(session, payload.description, payload.business_id, validate_remote, draft)


@router.post("/{flow_id}/generate", response_model=FlowGenerateOut)
async def regenerate(
    flow_id: str,
    payload: FlowRegenerateIn,
    scope: PanelScope = Depends(get_panel_scope),
    session: AsyncSession = Depends(get_session),
) -> FlowGenerateOut:
    """Regenera sobre un borrador existente (el mismo flow_id en Meta)."""
    row = await _owned_row(session, scope, flow_id)
    if row.status != service.STATUS_DRAFT:
        raise HTTPException(status_code=409, detail="Solo se regenera un borrador.")

    async def validate_remote(flow_json: dict[str, Any]) -> list[FlowIssue]:
        issues = await service.upload_for_validation(session, row, flow_json)
        await session.commit()
        return issues

    return await _generate(session, payload.description, row.business_id, validate_remote, [row])


async def _generate(session, description, business_id, validate_remote, draft) -> FlowGenerateOut:
    try:
        result = await generate_flow_json(
            description, business_id=business_id, validate_remote=validate_remote
        )
    except FlowGenerationUnavailable as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc
    except MetaGraphError as exc:
        await session.commit()
        raise HTTPException(status_code=502, detail=exc.detail) from exc
    except TemplateTargetError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc

    flow = draft[0] if draft else None
    if flow is not None:
        await session.refresh(flow)
    return FlowGenerateOut(
        flow=_to_out(flow) if flow is not None else None,
        flow_json=result.flow_json,
        issues=_issues_out(result.issues),
        attempts=result.attempts,
        ok=result.ok,
    )
