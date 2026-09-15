"""Espejo local de plantillas de WhatsApp y su ciclo de vida.

Meta es la fuente de verdad del ESTADO de una plantilla (la revision es
suya); este servicio mantiene `whatsapp_templates` como espejo con tres
entradas de informacion:

1. **Crear** desde el panel/API: se crea en Meta y la respuesta siembra la
   fila (id + status inicial, normalmente PENDING).
2. **Webhook** `message_template_status_update`: Meta avisa el cambio
   (APPROVED/REJECTED/PAUSED...) y aca se refleja - ver
   `apply_status_update_from_event()`, enganchado en api/webhooks.py.
3. **Sync manual**: `GET /{waba_id}/message_templates` upserta todo lo que
   exista alla (plantillas creadas por fuera del panel incluidas).

La WABA objetivo se resuelve igual que el envio: numero propio del negocio
(BusinessChannel) si se pide con `business_id`, o la WABA compartida de la
app. Una app sin `waba_id` configurado no puede gestionar plantillas - el
error lo dice explicito en vez de fallar raro contra Graph.
"""
from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from datetime import datetime
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from nexolu_comms_api.core.auth.apps import AppIdentity, resolve_by_app_id
from nexolu_comms_api.core.channels.business_channels import BusinessChannelRepository
from nexolu_comms_api.core.db.entities import BusinessChannel, WebhookEvent, WhatsAppTemplate
from nexolu_comms_api.core.db.session import get_sessionmaker
from nexolu_comms_api.core.meta.graph import MetaGraphClient

logger = logging.getLogger(__name__)


class TemplateTargetError(Exception):
    """No hay WABA/token con que operar plantillas para ese destino."""


@dataclass(frozen=True)
class TemplateTarget:
    """Contra que WABA (y con que token) se opera."""

    waba_id: str
    access_token: str
    business_channel_id: str | None  # None = WABA compartida de la app


async def resolve_target(
    session: AsyncSession, app: AppIdentity, business_id: str | None
) -> TemplateTarget:
    if business_id:
        channel = await BusinessChannelRepository(session).get_active_for_business(
            app.app_id, business_id
        )
        if channel is None:
            raise TemplateTargetError(
                f"El negocio '{business_id}' no tiene un numero propio activo."
            )
        return TemplateTarget(
            waba_id=channel.waba_id,
            access_token=channel.access_token,
            business_channel_id=channel.id,
        )

    if app.whatsapp is None or not app.whatsapp.waba_id:
        raise TemplateTargetError(
            f"La app '{app.app_id}' no tiene WABA configurada (falta waba_id en su credencial meta-whatsapp)."
        )
    return TemplateTarget(
        waba_id=app.whatsapp.waba_id,
        access_token=app.whatsapp.access_token,
        business_channel_id=None,
    )


class TemplateRepository:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def get(self, template_id: str) -> WhatsAppTemplate | None:
        return await self._session.get(WhatsAppTemplate, template_id)

    async def get_by_identity(
        self, waba_id: str, name: str, language: str
    ) -> WhatsAppTemplate | None:
        return (
            await self._session.execute(
                select(WhatsAppTemplate).where(
                    WhatsAppTemplate.waba_id == waba_id,
                    WhatsAppTemplate.name == name,
                    WhatsAppTemplate.language == language,
                )
            )
        ).scalar_one_or_none()

    async def get_for_send(
        self, app_id: str, waba_id: str | None, name: str, language: str
    ) -> WhatsAppTemplate | None:
        """Para validar un envio: por identidad exacta si conocemos la WABA,
        o por (app, name, language) si no."""
        query = select(WhatsAppTemplate).where(
            WhatsAppTemplate.app_id == app_id,
            WhatsAppTemplate.name == name,
            WhatsAppTemplate.language == language,
        )
        if waba_id:
            query = query.where(WhatsAppTemplate.waba_id == waba_id)
        return (await self._session.execute(query)).scalars().first()

    async def list_templates(
        self, app_id: str | None = None, business_channel_id: str | None = None
    ) -> list[WhatsAppTemplate]:
        query = select(WhatsAppTemplate).order_by(
            WhatsAppTemplate.name, WhatsAppTemplate.language
        )
        if app_id:
            query = query.where(WhatsAppTemplate.app_id == app_id)
        if business_channel_id:
            query = query.where(WhatsAppTemplate.business_channel_id == business_channel_id)
        return list((await self._session.execute(query)).scalars())

    async def upsert_from_meta(
        self,
        *,
        app_id: str,
        target: TemplateTarget,
        name: str,
        language: str,
        payload: dict[str, Any],
    ) -> WhatsAppTemplate:
        """Siembra/actualiza una fila con lo que Meta reporto de esa
        plantilla (respuesta de creacion o item del listado)."""
        row = await self.get_by_identity(target.waba_id, name, language)
        if row is None:
            row = WhatsAppTemplate(
                app_id=app_id,
                business_channel_id=target.business_channel_id,
                waba_id=target.waba_id,
                name=name,
                language=language,
                category=str(payload.get("category") or "UTILITY"),
            )
            self._session.add(row)

        if payload.get("id"):
            row.meta_template_id = str(payload["id"])
        if payload.get("status"):
            row.status = str(payload["status"])
        if payload.get("category"):
            row.category = str(payload["category"])
        if isinstance(payload.get("components"), list):
            row.components = payload["components"]
        quality = payload.get("quality_score")
        if isinstance(quality, dict) and quality.get("score"):
            row.quality_score = str(quality["score"])
        row.last_synced_at = datetime.utcnow()
        return row


async def sync_templates(
    session: AsyncSession, app: AppIdentity, target: TemplateTarget
) -> list[WhatsAppTemplate]:
    """Trae TODO lo que exista en la WABA (incluidas plantillas creadas por
    fuera del panel) y upserta el espejo."""
    graph = MetaGraphClient()
    repo = TemplateRepository(session)
    items = await graph.list_templates(target.waba_id, target.access_token)

    rows: list[WhatsAppTemplate] = []
    for item in items:
        name, language = item.get("name"), item.get("language")
        if not (name and language):
            continue
        rows.append(
            await repo.upsert_from_meta(
                app_id=app.app_id, target=target, name=str(name), language=str(language), payload=item
            )
        )
    await session.commit()
    return rows


async def apply_status_update_from_event(event_id: str) -> None:
    """Refleja en el espejo un webhook `message_template_status_update`.

    Corre como side-effect INTERNO despues de persistir el evento (el
    reenvio a la app duena no cambia: el payload le llega intacto). Nunca
    lanza: un payload con forma inesperada se loguea y se ignora - el
    espejo se puede reconciliar con un sync manual.
    """
    async with get_sessionmaker()() as session:
        event = await session.get(WebhookEvent, event_id)
        if event is None:
            return

        try:
            value = json.loads(event.payload)["entry"][0]["changes"][0]["value"]
        except (ValueError, KeyError, IndexError, TypeError):
            logger.warning("templates.status_update_unparseable", extra={"event_id": event_id})
            return

        new_status = value.get("event")
        name = value.get("message_template_name")
        language = value.get("message_template_language")
        meta_id = value.get("message_template_id")
        reason = value.get("reason")
        if not new_status:
            return

        repo = TemplateRepository(session)
        row: WhatsAppTemplate | None = None
        if meta_id is not None:
            row = (
                await session.execute(
                    select(WhatsAppTemplate).where(WhatsAppTemplate.meta_template_id == str(meta_id))
                )
            ).scalars().first()
        if row is None and name and language:
            # Fallback por identidad dentro de la WABA del evento (canal
            # propio) o de la app.
            waba_id = await _waba_for_event(session, event)
            if waba_id:
                row = await repo.get_by_identity(waba_id, str(name), str(language))

        if row is None:
            logger.info(
                "templates.status_update_without_mirror",
                extra={"event_id": event_id, "template_name": name, "template_status": new_status},
            )
            return

        row.status = str(new_status)
        if reason and str(reason).upper() != "NONE":
            row.reason = str(reason)
        row.last_synced_at = datetime.utcnow()
        await session.commit()
        logger.info(
            "templates.status_updated",
            extra={"template_id": row.id, "template_name": row.name, "template_status": row.status},
        )


async def _waba_for_event(session: AsyncSession, event: WebhookEvent) -> str | None:
    if event.business_channel_id:
        channel = await session.get(BusinessChannel, event.business_channel_id)
        return channel.waba_id if channel else None
    identity = await resolve_by_app_id(session, event.app_id)
    return identity.whatsapp.waba_id if identity and identity.whatsapp else None
