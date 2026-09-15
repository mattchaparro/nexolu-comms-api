"""Sincronizacion de catalogo contra Meta, por app o por negocio.

El principio 45 del brief manda aca: la app duena es la FUENTE DE VERDAD
del producto; el catalogo de Meta es su superficie comercial. Este modulo
recibe los productos normalizados que la app decide publicar, los traduce
al `items_batch` oficial (CREATE/UPDATE/DELETE por `retailer_id`) y guarda
el estado de sync por item en `catalog_items`.

Dos decisiones de eficiencia, por el rate limit oficial (~100 llamadas de
batch por hora por catalogo):

1. **content_hash**: un item cuyo contenido no cambio desde el ultimo sync
   exitoso NI SE ENVIA - re-sincronizar todo el catalogo del POS tras un
   cambio de un producto cuesta 1 item, no N.
2. **Un solo batch por llamada**: todos los cambios de una invocacion van
   en un `items_batch` (el limite oficial es 5.000 items por request).

El batch es asincrono del lado de Meta: la respuesta trae `handles` y los
items quedan `pending` hasta que `check_pending()` consulte
`check_batch_request_status` (invocable desde el panel o repetido por la
app). Los errores de validacion inmediatos (`validation_status`) si se
aplican en el momento.
"""
from __future__ import annotations

import hashlib
import json
import logging
from dataclasses import dataclass
from datetime import datetime
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from nexolu_comms_api.core.auth.apps import AppIdentity
from nexolu_comms_api.core.channels.business_channels import BusinessChannelRepository
from nexolu_comms_api.core.db.entities import CatalogItem
from nexolu_comms_api.core.meta.graph import MetaGraphClient

logger = logging.getLogger(__name__)


class CatalogTargetError(Exception):
    """No hay catalogo/token con que operar para ese destino."""


@dataclass(frozen=True)
class CatalogTarget:
    catalog_id: str
    access_token: str
    waba_id: str | None
    business_channel_id: str | None  # None = catalogo de la app (numero compartido)


async def resolve_target(
    session: AsyncSession, app: AppIdentity, business_id: str | None
) -> CatalogTarget:
    if business_id:
        channel = await BusinessChannelRepository(session).get_active_for_business(
            app.app_id, business_id
        )
        if channel is None:
            raise CatalogTargetError(f"El negocio '{business_id}' no tiene un numero propio activo.")
        if not channel.catalog_id:
            raise CatalogTargetError(
                f"El negocio '{business_id}' no tiene catalogo conectado - crearlo/conectarlo primero."
            )
        return CatalogTarget(
            catalog_id=channel.catalog_id,
            access_token=channel.access_token,
            waba_id=channel.waba_id,
            business_channel_id=channel.id,
        )

    if app.whatsapp is None or not app.whatsapp.catalog_id:
        raise CatalogTargetError(
            f"La app '{app.app_id}' no tiene catalogo conectado (falta catalog_id en su credencial meta-whatsapp)."
        )
    return CatalogTarget(
        catalog_id=app.whatsapp.catalog_id,
        access_token=app.whatsapp.access_token,
        waba_id=app.whatsapp.waba_id,
        business_channel_id=None,
    )


# Campos del `data` oficial del items_batch, en el orden del ejemplo de la
# doc. `id` ES el retailer_id (el mismo del webhook `order` y de SPM/MPM).
def _batch_data(item: dict[str, Any]) -> dict[str, Any]:
    data: dict[str, Any] = {
        "id": item["retailer_id"],
        "title": item["title"],
        "price": item["price"],
        "availability": item.get("availability") or "in stock",
        "condition": "new",
    }
    for src, dst in (("description", "description"), ("image_link", "image_link"), ("link", "link"), ("brand", "brand")):
        if item.get(src):
            data[dst] = item[src]
    return data


def _hash(data: dict[str, Any]) -> str:
    return hashlib.sha256(json.dumps(data, sort_keys=True, ensure_ascii=False).encode()).hexdigest()


class CatalogRepository:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def get(self, catalog_id: str, retailer_id: str) -> CatalogItem | None:
        return (
            await self._session.execute(
                select(CatalogItem).where(
                    CatalogItem.catalog_id == catalog_id, CatalogItem.retailer_id == retailer_id
                )
            )
        ).scalar_one_or_none()

    async def list_items(
        self, app_id: str | None = None, catalog_id: str | None = None
    ) -> list[CatalogItem]:
        query = select(CatalogItem).order_by(CatalogItem.updated_at.desc())
        if app_id:
            query = query.where(CatalogItem.app_id == app_id)
        if catalog_id:
            query = query.where(CatalogItem.catalog_id == catalog_id)
        return list((await self._session.execute(query)).scalars())


@dataclass(frozen=True)
class SyncOutcome:
    sent: int
    skipped: int
    deleted: int
    handle: str | None
    immediate_errors: list[str]


async def sync_items(
    session: AsyncSession,
    app: AppIdentity,
    target: CatalogTarget,
    items: list[dict[str, Any]],
    deletes: list[str],
) -> SyncOutcome:
    repo = CatalogRepository(session)
    requests: list[dict[str, Any]] = []
    sent = skipped = deleted = 0

    for item in items:
        data = _batch_data(item)
        content_hash = _hash(data)
        row = await repo.get(target.catalog_id, item["retailer_id"])

        if row is not None and row.content_hash == content_hash and row.sync_status == "synced":
            skipped += 1
            continue

        if row is None:
            row = CatalogItem(
                app_id=app.app_id,
                business_channel_id=target.business_channel_id,
                catalog_id=target.catalog_id,
                retailer_id=item["retailer_id"],
            )
            session.add(row)

        row.title = data["title"]
        row.description = data.get("description", "")
        row.price = data["price"]
        row.availability = data["availability"]
        row.image_link = data.get("image_link")
        row.link = data.get("link")
        row.brand = data.get("brand")
        row.content_hash = content_hash
        row.sync_status = "pending"
        row.last_error = None

        requests.append({"method": "UPDATE", "data": data})
        sent += 1

    for retailer_id in deletes:
        requests.append({"method": "DELETE", "data": {"id": retailer_id}})
        deleted += 1

    if not requests:
        await session.commit()
        return SyncOutcome(sent=0, skipped=skipped, deleted=0, handle=None, immediate_errors=[])

    response = await MetaGraphClient().items_batch(target.catalog_id, target.access_token, requests)
    handles = response.get("handles") or []
    handle = str(handles[0]) if handles else None

    # Errores de validacion inmediatos, por retailer_id (parseo defensivo:
    # la forma exacta puede variar entre versiones de la API).
    immediate_errors: list[str] = []
    for status_entry in response.get("validation_status") or []:
        if not isinstance(status_entry, dict):
            continue
        retailer_id = str(status_entry.get("retailer_id") or status_entry.get("id") or "")
        errors = status_entry.get("errors") or []
        messages = [str(e.get("message", e)) for e in errors if e]
        if not messages:
            continue
        immediate_errors.append(f"{retailer_id}: {'; '.join(messages)}")
        if retailer_id:
            row = await repo.get(target.catalog_id, retailer_id)
            if row is not None:
                row.sync_status = "error"
                row.last_error = "; ".join(messages)

    # Los pending de esta corrida quedan con el handle para el check.
    for item in items:
        row = await repo.get(target.catalog_id, item["retailer_id"])
        if row is not None and row.sync_status == "pending":
            row.batch_handle = handle

    for retailer_id in deletes:
        row = await repo.get(target.catalog_id, retailer_id)
        if row is not None:
            await session.delete(row)

    await session.commit()
    return SyncOutcome(sent=sent, skipped=skipped, deleted=deleted, handle=handle, immediate_errors=immediate_errors)


async def check_pending(
    session: AsyncSession, app: AppIdentity, target: CatalogTarget
) -> dict[str, int]:
    """Consulta los handles pendientes contra check_batch_request_status y
    resuelve cada item a `synced` o `error`. @return conteo por estado."""
    repo = CatalogRepository(session)
    rows = [
        row
        for row in await repo.list_items(app_id=app.app_id, catalog_id=target.catalog_id)
        if row.sync_status == "pending" and row.batch_handle
    ]
    if not rows:
        return {"synced": 0, "errors": 0, "still_pending": 0}

    graph = MetaGraphClient()
    synced = errors = still_pending = 0
    by_handle: dict[str, list[CatalogItem]] = {}
    for row in rows:
        by_handle.setdefault(row.batch_handle or "", []).append(row)

    for handle, handle_rows in by_handle.items():
        response = await graph.check_batch_status(target.catalog_id, target.access_token, handle)
        entries = response.get("data") or []
        entry = entries[0] if entries and isinstance(entries[0], dict) else {}
        status = str(entry.get("status") or "").lower()
        error_list = entry.get("errors") or []
        errors_by_retailer: dict[str, str] = {}
        general_errors: list[str] = []
        for error in error_list:
            if not isinstance(error, dict):
                continue
            retailer_id = str(error.get("retailer_id") or error.get("id") or "")
            message = str(error.get("message") or error)
            if retailer_id:
                errors_by_retailer[retailer_id] = message
            else:
                general_errors.append(message)

        for row in handle_rows:
            if row.retailer_id in errors_by_retailer:
                row.sync_status = "error"
                row.last_error = errors_by_retailer[row.retailer_id]
                errors += 1
            elif status == "finished" and not general_errors:
                row.sync_status = "synced"
                row.last_error = None
                row.last_synced_at = datetime.utcnow()
                synced += 1
            elif general_errors:
                row.sync_status = "error"
                row.last_error = "; ".join(general_errors)
                errors += 1
            else:
                still_pending += 1

    await session.commit()
    return {"synced": synced, "errors": errors, "still_pending": still_pending}
