"""Cliente minimo de Graph API: onboarding de numeros propios (Embedded
Signup), gestion de plantillas de mensaje y de WhatsApp Flows. Solo las llamadas que los
flujos necesitan - ver el analisis
(`nexolu-utils/docs/research/whatsapp-capacidad-transversal.md`, secciones
F e I) con sus fuentes oficiales. El envio de mensajes NO pasa por aca
(ese ya vive en core/channels/whatsapp.py); cuando llegue el modulo de
catalogo (fase 4 del plan), sus llamadas se agregan a este cliente, no
dispersas.
"""
from __future__ import annotations

import logging
from typing import Any

import httpx

from nexolu_comms_api.config import Settings, get_settings

logger = logging.getLogger(__name__)


class MetaGraphError(Exception):
    """Meta rechazo la llamada (o no respondio). `detail` es apto para
    mostrarselo al operador; el body crudo queda en el log."""

    def __init__(self, detail: str, status_code: int | None = None) -> None:
        super().__init__(detail)
        self.detail = detail
        self.status_code = status_code


class MetaGraphClient:
    def __init__(self, settings: Settings | None = None) -> None:
        self._settings = settings or get_settings()

    async def exchange_code(self, code: str) -> str:
        """Cambia el `code` que devuelve el popup de Embedded Signup por el
        business integration system user access token del cliente."""
        data = await self._request(
            "GET",
            "/oauth/access_token",
            params={
                "client_id": self._settings.meta_platform_app_id,
                "client_secret": self._settings.meta_platform_app_secret,
                "code": code,
            },
        )
        token = data.get("access_token")
        if not token:
            raise MetaGraphError("Meta no devolvio access_token al intercambiar el code.")
        return token

    async def subscribe_app(self, waba_id: str, access_token: str) -> None:
        """Suscribe la app Meta de plataforma a los webhooks de esa WABA."""
        await self._request("POST", f"/{waba_id}/subscribed_apps", token=access_token)

    async def register_phone(self, phone_number_id: str, access_token: str, pin: str) -> None:
        """Activa el numero en Cloud API, fijando el PIN de dos pasos."""
        await self._request(
            "POST",
            f"/{phone_number_id}/register",
            token=access_token,
            json={"messaging_product": "whatsapp", "pin": pin},
        )

    # -- plantillas de mensaje (POST/GET/DELETE /{waba_id}/message_templates,
    # permiso whatsapp_business_management; limite oficial: 100 creaciones
    # por hora por WABA) ------------------------------------------------------

    async def create_template(
        self,
        waba_id: str,
        access_token: str,
        *,
        name: str,
        language: str,
        category: str,
        components: list[dict[str, Any]],
    ) -> dict[str, Any]:
        """@return la respuesta de Meta: {id, status, category}."""
        return await self._request(
            "POST",
            f"/{waba_id}/message_templates",
            token=access_token,
            json={"name": name, "language": language, "category": category, "components": components},
        )

    async def list_templates(self, waba_id: str, access_token: str) -> list[dict[str, Any]]:
        """Primera pagina (hasta 200): suficiente para el espejo de un
        negocio normal; si alguna WABA supera eso, este es el punto unico
        donde agregar paginacion por `paging.next`."""
        data = await self._request(
            "GET",
            f"/{waba_id}/message_templates",
            token=access_token,
            params={
                "fields": "id,name,language,status,category,components,quality_score",
                "limit": "200",
            },
        )
        items = data.get("data")
        return items if isinstance(items, list) else []

    # -- catalogo y comercio (fuentes oficiales en el analisis, seccion F:
    # Product Catalog reference, items_batch, WABA product_catalogs edge;
    # limites: ~100 batches/hora por catalogo, hasta 5.000 items/request) ----

    async def create_catalog(
        self, meta_business_id: str, access_token: str, *, name: str, vertical: str = "commerce"
    ) -> str:
        """POST /{business_id}/owned_product_catalogs. @return catalog_id.
        OJO (condicion oficial): los Terminos de catalogo se aceptan creando
        el PRIMER catalogo del negocio via Business Manager - si nunca se ha
        hecho, esta llamada falla y ese paso manual es del cliente."""
        data = await self._request(
            "POST",
            f"/{meta_business_id}/owned_product_catalogs",
            token=access_token,
            json={"name": name, "vertical": vertical},
        )
        catalog_id = data.get("id")
        if not catalog_id:
            raise MetaGraphError("Meta no devolvio el id del catalogo creado.")
        return str(catalog_id)

    async def connect_catalog_to_waba(self, waba_id: str, access_token: str, catalog_id: str) -> None:
        """POST /{waba_id}/product_catalogs. Regla de Meta: UN catalogo
        conectado por WABA."""
        await self._request(
            "POST",
            f"/{waba_id}/product_catalogs",
            token=access_token,
            json={"catalog_id": catalog_id},
        )

    async def items_batch(
        self, catalog_id: str, access_token: str, requests: list[dict[str, Any]], *, allow_upsert: bool = True
    ) -> dict[str, Any]:
        """POST /{catalog_id}/items_batch. @return la respuesta cruda de
        Meta ({handles: [...], validation_status: [...]})."""
        return await self._request(
            "POST",
            f"/{catalog_id}/items_batch",
            token=access_token,
            json={"item_type": "PRODUCT_ITEM", "allow_upsert": allow_upsert, "requests": requests},
        )

    async def check_batch_status(self, catalog_id: str, access_token: str, handle: str) -> dict[str, Any]:
        """GET /{catalog_id}/check_batch_request_status por handle."""
        return await self._request(
            "GET",
            f"/{catalog_id}/check_batch_request_status",
            token=access_token,
            params={"handle": handle},
        )

    async def delete_template(self, waba_id: str, access_token: str, name: str) -> None:
        """OJO (comportamiento oficial de Meta): borra la plantilla `name`
        en TODOS sus idiomas de esa WABA - no hay borrado por idioma."""
        await self._request(
            "DELETE",
            f"/{waba_id}/message_templates",
            token=access_token,
            params={"name": name},
        )

    # -- WhatsApp Flows ("Formularios" en el panel). Referencia oficial:
    # developers.facebook.com/docs/whatsapp/flows/reference/flowsapi - mismo
    # token y permiso que las plantillas (whatsapp_business_management).
    # Ciclo: crear borrador -> subir flow.json (la respuesta trae los
    # validation_errors) -> publicar. Un Flow publicado ya no se edita. ---

    async def create_flow(
        self, waba_id: str, access_token: str, *, name: str, categories: list[str]
    ) -> str:
        """POST /{waba_id}/flows (sin flow_json: el JSON se sube aparte como
        asset, que es lo que devuelve los errores de validacion).
        @return el flow_id del borrador."""
        data = await self._request(
            "POST",
            f"/{waba_id}/flows",
            token=access_token,
            json={"name": name, "categories": categories},
        )
        flow_id = data.get("id")
        if not flow_id:
            raise MetaGraphError("Meta no devolvio el id del formulario creado.")
        return str(flow_id)

    async def upload_flow_json(
        self, flow_id: str, access_token: str, flow_json: str
    ) -> list[dict[str, Any]]:
        """POST /{flow_id}/assets, multipart: file=flow.json,
        name=flow.json, asset_type=FLOW_JSON. Solo sobre borradores.
        @return los validation_errors de Meta (vacia = valido)."""
        data = await self._request(
            "POST",
            f"/{flow_id}/assets",
            token=access_token,
            data={"name": "flow.json", "asset_type": "FLOW_JSON"},
            files={"file": ("flow.json", flow_json.encode("utf-8"), "application/json")},
        )
        errors = data.get("validation_errors")
        return errors if isinstance(errors, list) else []

    async def get_flow(self, flow_id: str, access_token: str) -> dict[str, Any]:
        """GET /{flow_id} con estado, errores y la URL de vista previa.
        `preview.invalidate(false)` reusa el link vigente (vence a los 30
        dias) en vez de invalidar el que el panel ya tenga abierto."""
        return await self._request(
            "GET",
            f"/{flow_id}",
            token=access_token,
            params={
                "fields": "id,name,status,categories,validation_errors,json_version,"
                "preview.invalidate(false)",
            },
        )

    async def list_flows(self, waba_id: str, access_token: str) -> list[dict[str, Any]]:
        """Primera pagina, igual que las plantillas."""
        data = await self._request(
            "GET",
            f"/{waba_id}/flows",
            token=access_token,
            params={"fields": "id,name,status,categories,validation_errors", "limit": "200"},
        )
        items = data.get("data")
        return items if isinstance(items, list) else []

    async def download_flow_json(self, flow_id: str, access_token: str) -> dict[str, Any] | None:
        """El JSON de un Flow creado por fuera del panel: GET
        /{flow_id}/assets trae un `download_url` firmado del FLOW_JSON.
        @return el JSON, o None si no hay asset o no se pudo leer."""
        data = await self._request("GET", f"/{flow_id}/assets", token=access_token)
        for asset in data.get("data") or []:
            if asset.get("asset_type") == "FLOW_JSON" and asset.get("download_url"):
                try:
                    async with httpx.AsyncClient(
                        timeout=self._settings.http_timeout_seconds
                    ) as client:
                        response = await client.get(asset["download_url"])
                    response.raise_for_status()
                    body = response.json()
                except (httpx.HTTPError, ValueError):
                    logger.warning("meta_graph.flow_json_unreadable", extra={"flow_id": flow_id})
                    return None
                return body if isinstance(body, dict) else None
        return None

    async def publish_flow(self, flow_id: str, access_token: str) -> None:
        await self._request("POST", f"/{flow_id}/publish", token=access_token)

    async def deprecate_flow(self, flow_id: str, access_token: str) -> None:
        await self._request("POST", f"/{flow_id}/deprecate", token=access_token)

    async def delete_flow(self, flow_id: str, access_token: str) -> None:
        """Solo borradores (regla de Meta): uno publicado se depreca."""
        await self._request("DELETE", f"/{flow_id}", token=access_token)

    async def _request(
        self,
        method: str,
        path: str,
        *,
        token: str | None = None,
        params: dict[str, str] | None = None,
        json: dict[str, Any] | None = None,
        data: dict[str, str] | None = None,
        files: dict[str, tuple[str, bytes, str]] | None = None,
    ) -> dict[str, Any]:
        url = f"{self._settings.whatsapp_api_base_url}{path}"
        headers = {"Authorization": f"Bearer {token}"} if token else {}

        try:
            async with httpx.AsyncClient(timeout=self._settings.http_timeout_seconds) as client:
                response = await client.request(
                    method, url, params=params, json=json, data=data, files=files, headers=headers
                )
        except httpx.HTTPError as exc:
            raise MetaGraphError(f"No se pudo contactar a Graph API: {exc}") from exc

        if response.is_error:
            detail = _error_detail(response)
            logger.warning(
                "meta_graph.rejected",
                extra={"path": path, "status_code": response.status_code, "detail": detail},
            )
            raise MetaGraphError(detail, status_code=response.status_code)

        try:
            return response.json()
        except ValueError:
            return {}


def _error_detail(response: httpx.Response) -> str:
    try:
        error = response.json().get("error", {})
        message = error.get("message") or "Error de Graph API."
        return f"Meta respondio {response.status_code}: {message}"
    except ValueError:
        return f"Meta respondio {response.status_code}."
