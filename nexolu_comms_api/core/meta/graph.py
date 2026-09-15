"""Cliente minimo de Graph API para el onboarding de numeros propios
(Embedded Signup). Solo las tres llamadas que el flujo necesita - ver el
analisis (`nexolu-utils/docs/research/whatsapp-capacidad-transversal.md`,
seccion I) y la doc oficial de Meta "Onboarding customers as a Tech
Provider". El envio de mensajes NO pasa por aca (ese ya vive en
core/channels/whatsapp.py); cuando llegue el modulo de catalogo (fase 4 del
plan), sus llamadas se agregan a este cliente, no dispersas.
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

    async def _request(
        self,
        method: str,
        path: str,
        *,
        token: str | None = None,
        params: dict[str, str] | None = None,
        json: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        url = f"{self._settings.whatsapp_api_base_url}{path}"
        headers = {"Authorization": f"Bearer {token}"} if token else {}

        try:
            async with httpx.AsyncClient(timeout=self._settings.http_timeout_seconds) as client:
                response = await client.request(method, url, params=params, json=json, headers=headers)
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
