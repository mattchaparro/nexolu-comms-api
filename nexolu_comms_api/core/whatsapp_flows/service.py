"""Ciclo de vida de los formularios de WhatsApp (WhatsApp Flows) contra Meta.

Mismo modelo que las plantillas (core/templates/service.py): Meta es la
fuente de verdad del estado y de la validacion; `whatsapp_flows` es el
espejo que el panel lista y edita. La WABA objetivo se resuelve igual
(`resolve_target`: numero propio del negocio o WABA compartida de la app).

Reglas de Meta que dan forma a esto:

- Un Flow se crea como BORRADOR y el JSON se sube aparte (asset
  `flow.json`): esa subida es la que devuelve los `validation_errors`.
- Solo se publica un borrador sin errores, y uno PUBLICADO ya no se edita
  (ni el JSON ni se borra): se depreca. Cambiar un formulario publicado es
  crear otro borrador.

Antes de cada subida corre el validador local (validator.py): un JSON que
ni siquiera tiene la forma correcta no gasta una llamada a Meta.
"""
from __future__ import annotations

import json
import logging
from datetime import datetime
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from nexolu_comms_api.core.auth.apps import AppIdentity, resolve_by_app_id
from nexolu_comms_api.core.db.entities import BusinessChannel, WhatsAppFlow
from nexolu_comms_api.core.meta.graph import MetaGraphClient
from nexolu_comms_api.core.templates.service import (
    TemplateTarget,
    TemplateTargetError,
    resolve_target,
)
from nexolu_comms_api.core.whatsapp_flows.validator import (
    FlowIssue,
    has_errors,
    meta_issues,
    validate_flow_json,
)

logger = logging.getLogger(__name__)

STATUS_DRAFT = "DRAFT"
STATUS_PUBLISHED = "PUBLISHED"
STATUS_DEPRECATED = "DEPRECATED"

CATEGORIES = (
    "SIGN_UP",
    "SIGN_IN",
    "APPOINTMENT_BOOKING",
    "LEAD_GENERATION",
    "CONTACT_US",
    "CUSTOMER_SUPPORT",
    "SURVEY",
    "OTHER",
)


class FlowValidationError(Exception):
    """El validador local encontro errores: no se llamo a Meta."""

    def __init__(self, issues: list[FlowIssue]) -> None:
        super().__init__("El Flow JSON tiene errores.")
        self.issues = issues


class FlowStateError(Exception):
    """La operacion no aplica al estado actual (p.ej. editar uno publicado)."""


class FlowRepository:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def get(self, flow_id: str) -> WhatsAppFlow | None:
        return await self._session.get(WhatsAppFlow, flow_id)

    async def get_by_name(self, waba_id: str, name: str) -> WhatsAppFlow | None:
        return (
            await self._session.execute(
                select(WhatsAppFlow).where(WhatsAppFlow.waba_id == waba_id, WhatsAppFlow.name == name)
            )
        ).scalar_one_or_none()

    async def get_by_meta_id(self, meta_flow_id: str) -> WhatsAppFlow | None:
        return (
            await self._session.execute(
                select(WhatsAppFlow).where(WhatsAppFlow.meta_flow_id == meta_flow_id)
            )
        ).scalars().first()

    async def get_by_library_key(self, waba_id: str, library_key: str) -> WhatsAppFlow | None:
        """El vigente (no deprecado) de esa plantilla en esa WABA."""
        return (
            await self._session.execute(
                select(WhatsAppFlow)
                .where(
                    WhatsAppFlow.waba_id == waba_id,
                    WhatsAppFlow.library_key == library_key,
                    WhatsAppFlow.status != STATUS_DEPRECATED,
                )
                .order_by(WhatsAppFlow.created_at.desc())
            )
        ).scalars().first()

    async def list_flows(self, app_id: str | None = None) -> list[WhatsAppFlow]:
        query = select(WhatsAppFlow).order_by(WhatsAppFlow.created_at.desc())
        if app_id:
            query = query.where(WhatsAppFlow.app_id == app_id)
        return list((await self._session.execute(query)).scalars())


def serialize(flow_json: dict[str, Any]) -> str:
    """Lo que se sube a Meta: indentado, para que la linea/columna de un
    validation_error apunte a algo legible en el editor del panel (que
    muestra el mismo JSON con la misma indentacion)."""
    return json.dumps(flow_json, ensure_ascii=False, indent=2)


def parse_stored(row: WhatsAppFlow) -> dict[str, Any] | None:
    if not row.flow_json:
        return None
    try:
        parsed = json.loads(row.flow_json)
    except ValueError:
        return None
    return parsed if isinstance(parsed, dict) else None


def check_locally(flow_json: Any) -> list[FlowIssue]:
    """@raise FlowValidationError si hay errores; @return las advertencias."""
    issues = validate_flow_json(flow_json)
    if has_errors(issues):
        raise FlowValidationError(issues)
    return issues


async def token_for(session: AsyncSession, row: WhatsAppFlow) -> str:
    """El token del dueno de la WABA de la fila (canal propio o app)."""
    if row.business_channel_id:
        channel = await session.get(BusinessChannel, row.business_channel_id)
        if channel is None:
            raise TemplateTargetError("El canal de este formulario ya no existe.")
        return channel.access_token
    app = await resolve_by_app_id(session, row.app_id)
    if app is None:
        raise TemplateTargetError(f"La app '{row.app_id}' ya no existe.")
    return (await resolve_target(session, app, None)).access_token


async def create_draft(
    session: AsyncSession,
    *,
    app: AppIdentity,
    target: TemplateTarget,
    business_id: str | None,
    name: str,
    categories: list[str],
    flow_json: dict[str, Any],
    library_key: str | None = None,
) -> WhatsAppFlow:
    """Valida local, crea el borrador en Meta, sube el JSON y deja la fila
    con los errores de Meta y la vista previa. NO hace commit.
    @raise FlowValidationError, MetaGraphError."""
    warnings = check_locally(flow_json)
    graph = MetaGraphClient()
    meta_flow_id = await graph.create_flow(
        target.waba_id, target.access_token, name=name, categories=categories
    )
    row = WhatsAppFlow(
        app_id=app.app_id,
        business_id=business_id,
        business_channel_id=target.business_channel_id,
        waba_id=target.waba_id,
        meta_flow_id=meta_flow_id,
        name=name,
        categories=list(categories),
        status=STATUS_DRAFT,
        library_key=library_key,
        validation_errors=[],
    )
    session.add(row)
    await _upload(row, target.access_token, flow_json, warnings)
    return row


async def update_json(
    session: AsyncSession, row: WhatsAppFlow, flow_json: dict[str, Any]
) -> WhatsAppFlow:
    """Sube un JSON nuevo a un borrador. @raise FlowStateError,
    FlowValidationError, MetaGraphError."""
    if row.status != STATUS_DRAFT:
        raise FlowStateError(
            "Un formulario publicado o deprecado ya no se edita (regla de Meta): crea uno nuevo."
        )
    warnings = check_locally(flow_json)
    await _upload(row, await token_for(session, row), flow_json, warnings)
    return row


async def upload_for_validation(
    session: AsyncSession, row: WhatsAppFlow, flow_json: dict[str, Any]
) -> list[FlowIssue]:
    """Sube un JSON YA validado localmente y devuelve solo lo que dijo Meta
    (lo usa el generador con IA en su bucle de reintentos)."""
    await _upload(row, await token_for(session, row), flow_json, validate_flow_json(flow_json))
    return [FlowIssue(**issue) for issue in row.validation_errors if issue.get("source") == "meta"]


async def _upload(
    row: WhatsAppFlow, token: str, flow_json: dict[str, Any], warnings: list[FlowIssue]
) -> None:
    graph = MetaGraphClient()
    errors = await graph.upload_flow_json(row.meta_flow_id or "", token, serialize(flow_json))
    row.flow_json = serialize(flow_json)
    row.json_version = str(flow_json.get("version") or "") or None
    row.validation_errors = [issue.to_dict() for issue in warnings + meta_issues(errors)]
    await _refresh_from_meta(row, token, keep_errors=True)


async def refresh(session: AsyncSession, row: WhatsAppFlow) -> WhatsAppFlow:
    """Relee de Meta estado, errores y vista previa."""
    await _refresh_from_meta(row, await token_for(session, row))
    return row


async def _refresh_from_meta(row: WhatsAppFlow, token: str, *, keep_errors: bool = False) -> None:
    if not row.meta_flow_id:
        return
    data = await MetaGraphClient().get_flow(row.meta_flow_id, token)
    _apply_meta(row, data, keep_errors=keep_errors)


def _apply_meta(row: WhatsAppFlow, data: dict[str, Any], *, keep_errors: bool = False) -> None:
    if data.get("status"):
        row.status = str(data["status"])
    if isinstance(data.get("categories"), list):
        row.categories = [str(c) for c in data["categories"]]
    if data.get("json_version"):
        row.json_version = str(data["json_version"])
    preview = data.get("preview")
    if isinstance(preview, dict) and preview.get("preview_url"):
        row.preview_url = str(preview["preview_url"])
        row.preview_expires_at = str(preview.get("expires_at") or "") or None
    if not keep_errors and isinstance(data.get("validation_errors"), list):
        local = [issue for issue in row.validation_errors or [] if issue.get("source") == "local"]
        row.validation_errors = local + [
            issue.to_dict() for issue in meta_issues(data["validation_errors"])
        ]
    row.last_synced_at = datetime.utcnow()


async def publish(session: AsyncSession, row: WhatsAppFlow) -> WhatsAppFlow:
    if row.status != STATUS_DRAFT:
        raise FlowStateError(f"Solo se publica un borrador (este esta {row.status}).")
    if any(issue.get("severity", "error") == "error" for issue in row.validation_errors or []):
        raise FlowStateError("Tiene errores de validacion: corrigelos antes de publicar.")
    token = await token_for(session, row)
    await MetaGraphClient().publish_flow(row.meta_flow_id or "", token)
    row.published_at = datetime.utcnow()
    await _refresh_from_meta(row, token)
    # Meta confirmo el publish: si la lectura inmediata todavia dice DRAFT
    # (consistencia eventual), no se retrocede - el proximo refresco lo
    # confirma.
    if row.status == STATUS_DRAFT:
        row.status = STATUS_PUBLISHED
    return row


async def deprecate(session: AsyncSession, row: WhatsAppFlow) -> WhatsAppFlow:
    if row.status != STATUS_PUBLISHED:
        raise FlowStateError("Solo se depreca un formulario publicado; un borrador se elimina.")
    token = await token_for(session, row)
    await MetaGraphClient().deprecate_flow(row.meta_flow_id or "", token)
    row.status = STATUS_DEPRECATED
    row.last_synced_at = datetime.utcnow()
    return row


async def delete_draft(session: AsyncSession, row: WhatsAppFlow) -> None:
    if row.status != STATUS_DRAFT:
        raise FlowStateError("Meta solo deja eliminar borradores: uno publicado se depreca.")
    if row.meta_flow_id:
        await MetaGraphClient().delete_flow(row.meta_flow_id, await token_for(session, row))
    await session.delete(row)


async def sync_flows(
    session: AsyncSession, app: AppIdentity, target: TemplateTarget, business_id: str | None
) -> list[WhatsAppFlow]:
    """Trae TODO lo que exista en la WABA (incluidos formularios creados en
    el WhatsApp Manager) y upserta el espejo. El JSON de los que no
    conocemos se descarga del asset: el listado de Meta no lo trae."""
    graph = MetaGraphClient()
    repo = FlowRepository(session)
    rows: list[WhatsAppFlow] = []
    for item in await graph.list_flows(target.waba_id, target.access_token):
        meta_flow_id, name = item.get("id"), item.get("name")
        if not (meta_flow_id and name):
            continue
        row = await repo.get_by_meta_id(str(meta_flow_id))
        if row is None:
            row = await repo.get_by_name(target.waba_id, str(name))
        if row is None:
            row = WhatsAppFlow(
                app_id=app.app_id,
                business_id=business_id,
                business_channel_id=target.business_channel_id,
                waba_id=target.waba_id,
                name=str(name),
                categories=[],
                validation_errors=[],
            )
            session.add(row)
        row.meta_flow_id = str(meta_flow_id)
        row.name = str(name)
        _apply_meta(row, item)
        if row.flow_json is None:
            downloaded = await graph.download_flow_json(str(meta_flow_id), target.access_token)
            if downloaded is not None:
                row.flow_json = serialize(downloaded)
                row.json_version = str(downloaded.get("version") or "") or row.json_version
        rows.append(row)
    await session.commit()
    return rows
