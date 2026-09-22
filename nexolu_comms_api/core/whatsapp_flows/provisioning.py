"""Formularios de la biblioteca: crearlos (y publicarlos) en una WABA.

Dos entradas:

- **Panel** ("Crear desde plantilla"): el operador elige app/negocio y
  plantilla - ver `provision_from_library()`.
- **Auto-provision** al conectar el numero propio de un negocio (Embedded
  Signup, api/v1/onboarding.py): si la app lo pidio en
  `WHATSAPP_FLOW_AUTOPROVISION` ({"spa": ["confirm_booking"]}), se crea, se
  publica y se le avisa el flow_id a la app duena con el evento firmado
  `whatsapp_flow_provisioned` - la app guarda ese id por negocio y desde
  ahi puede mandar el formulario. Mejor-esfuerzo: si Meta falla, el canal
  igual queda conectado y el formulario se crea despues desde el panel.

Idempotente por (WABA, plantilla): si ya hay uno vigente de esa plantilla
en esa WABA, se reusa (y se publica si seguia en borrador) en vez de chocar
con el nombre unico de Meta.
"""
from __future__ import annotations

import json
import logging

from sqlalchemy.ext.asyncio import AsyncSession

from nexolu_comms_api.config import get_settings
from nexolu_comms_api.core.auth.apps import AppIdentity, resolve_by_app_id
from nexolu_comms_api.core.db.entities import WhatsAppFlow
from nexolu_comms_api.core.db.session import get_sessionmaker
from nexolu_comms_api.core.meta.graph import MetaGraphError
from nexolu_comms_api.core.templates.service import (
    TemplateTarget,
    TemplateTargetError,
    resolve_target,
)
from nexolu_comms_api.core.webhooks.app_events import post_app_event
from nexolu_comms_api.core.whatsapp_flows import service
from nexolu_comms_api.core.whatsapp_flows.library import LibraryEntry, fresh_json, get_entry

logger = logging.getLogger(__name__)

EVENT_PROVISIONED = "whatsapp_flow_provisioned"


async def provision_from_library(
    session: AsyncSession,
    *,
    app: AppIdentity,
    target: TemplateTarget,
    business_id: str | None,
    entry: LibraryEntry,
    publish: bool,
) -> WhatsAppFlow:
    """Hace commit del borrador ANTES de publicar: si publicar falla, el
    borrador ya existe en Meta y tiene que quedar en el espejo (si no, el
    reintento chocaria con su nombre). @raise FlowValidationError,
    FlowStateError, MetaGraphError."""
    repo = service.FlowRepository(session)
    row = await repo.get_by_library_key(target.waba_id, entry.key)
    if row is None:
        name = entry.name
        # El nombre es unico por WABA: si alguien ya uso "confirmar_cita"
        # para otra cosa, se sufija en vez de fallar.
        suffix = 2
        while await repo.get_by_name(target.waba_id, name) is not None:
            name = f"{entry.name}_{suffix}"
            suffix += 1
        row = await service.create_draft(
            session,
            app=app,
            target=target,
            business_id=business_id,
            name=name,
            categories=list(entry.categories),
            flow_json=fresh_json(entry),
            library_key=entry.key,
        )
        await session.commit()
    if publish and row.status == service.STATUS_DRAFT:
        await service.publish(session, row)
    return row


def autoprovision_keys(app_id: str) -> list[str]:
    try:
        config = json.loads(get_settings().whatsapp_flow_autoprovision or "{}")
    except ValueError:
        logger.warning("whatsapp_flows.autoprovision_config_invalid")
        return []
    keys = config.get(app_id) if isinstance(config, dict) else None
    return [str(key) for key in keys] if isinstance(keys, list) else []


async def autoprovision_for_channel(app_id: str, business_id: str) -> None:
    """Corre en segundo plano despues de conectar el canal. Nunca lanza."""
    keys = autoprovision_keys(app_id)
    if not keys:
        return

    async with get_sessionmaker()() as session:
        app = await resolve_by_app_id(session, app_id)
        if app is None:
            return
        try:
            target = await resolve_target(session, app, business_id)
        except TemplateTargetError as exc:
            logger.warning("whatsapp_flows.autoprovision_no_target", extra={"detail": str(exc)})
            return

        for key in keys:
            entry = get_entry(key)
            if entry is None:
                logger.warning("whatsapp_flows.autoprovision_unknown_key", extra={"key": key})
                continue
            try:
                row = await provision_from_library(
                    session, app=app, target=target, business_id=business_id, entry=entry, publish=True
                )
                await session.commit()
            except (MetaGraphError, service.FlowValidationError, service.FlowStateError) as exc:
                await session.rollback()
                logger.warning(
                    "whatsapp_flows.autoprovision_failed",
                    extra={"app_id": app_id, "business_id": business_id, "key": key, "detail": str(exc)},
                )
                continue

            logger.info(
                "whatsapp_flows.autoprovisioned",
                extra={"app_id": app_id, "business_id": business_id, "key": key, "flow_id": row.meta_flow_id},
            )
            await post_app_event(
                app.whatsapp,
                EVENT_PROVISIONED,
                {
                    "business_id": business_id,
                    "library_key": key,
                    "flow_id": row.meta_flow_id,
                    "flow_name": row.name,
                    "status": row.status,
                },
            )
