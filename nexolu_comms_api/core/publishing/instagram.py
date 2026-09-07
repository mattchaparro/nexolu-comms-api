"""Publicar en Instagram: historias, por ahora.

NO es un `ChannelSender`, y eso es deliberado. Un canal manda algo A ALGUIEN
-- tiene `to`, tiene ventana de 24h, tiene costo por conversacion. Una
historia no tiene destinatario: es una publicacion. Meterla en el mismo
molde obligaria a inventarle un `to` falso y a que cada canal existente
ignore la mitad de los campos nuevos.

Diferencia importante con WhatsApp, que conviene tener escrita: los ESTADOS
de WhatsApp no se pueden publicar por API -- Meta no expone endpoint y su
tabla de Coexistence los marca "Not supported". Las historias de Instagram
si, desde 2023, con `media_type=STORIES`.

Lo que la API NO permite en una historia, aunque la app movil si:
stickers, enlaces, encuestas, ubicacion, musica y texto encima. Sale
"pelada". Se pueden mencionar cuentas (`user_tags`, desde julio 2025).
"""
from __future__ import annotations

import logging
from dataclasses import dataclass

import httpx

from nexolu_comms_api.config import Settings, get_settings
from nexolu_comms_api.core.auth.apps import AppIdentity

logger = logging.getLogger(__name__)

STATUS_PUBLISHED = "published"
STATUS_FAILED = "failed"
STATUS_SKIPPED = "skipped"


@dataclass(frozen=True)
class PublishResult:
    status: str
    media_id: str | None = None
    error: str | None = None


class InstagramPublisher:
    """Publica una historia en la cuenta de Instagram de una app.

    Son DOS llamadas, no una: primero se crea un contenedor con la URL del
    medio y despues se publica. Meta descarga la imagen en el paso de
    publicacion, asi que la URL tiene que ser publica y seguir viva en ese
    momento -- una URL firmada que expire entre los dos pasos falla aqui, no
    al crearla.
    """

    def __init__(self, settings: Settings | None = None) -> None:
        self._settings = settings or get_settings()

    def is_configured(self, app: AppIdentity) -> bool:
        return app.instagram is not None

    async def publish_story(
        self,
        app: AppIdentity,
        *,
        image_url: str | None = None,
        video_url: str | None = None,
        mentions: list[str] | None = None,
    ) -> PublishResult:
        if app.instagram is None:
            return PublishResult(status=STATUS_SKIPPED, error="Esta app no tiene Instagram configurado.")

        if not image_url and not video_url:
            return PublishResult(status=STATUS_FAILED, error="Hace falta image_url o video_url.")

        container, error = await self._create_container(app, image_url, video_url, mentions)

        if error is not None:
            return error

        return await self._publish(app, container)

    async def _create_container(
        self,
        app: AppIdentity,
        image_url: str | None,
        video_url: str | None,
        mentions: list[str] | None,
    ) -> tuple[str, None] | tuple[None, PublishResult]:
        assert app.instagram is not None

        params: dict[str, str] = {"media_type": "STORIES"}

        if image_url:
            params["image_url"] = image_url
        else:
            params["video_url"] = video_url or ""

        if mentions:
            # Lo unico que la API permite encima de una historia. Sin
            # coordenadas: en historias son opcionales y Meta las coloca.
            import json

            params["user_tags"] = json.dumps([{"username": m.lstrip("@")} for m in mentions])

        response, failure = await self._post(app, f"{app.instagram.ig_user_id}/media", params)

        if failure is not None:
            return None, failure

        assert response is not None
        container_id = response.json().get("id")

        if not container_id:
            return None, PublishResult(status=STATUS_FAILED, error="Meta no devolvió el id del contenedor.")

        return container_id, None

    async def _publish(self, app: AppIdentity, container_id: str) -> PublishResult:
        assert app.instagram is not None

        response, failure = await self._post(
            app,
            f"{app.instagram.ig_user_id}/media_publish",
            {"creation_id": container_id},
        )

        if failure is not None:
            return failure

        assert response is not None

        return PublishResult(status=STATUS_PUBLISHED, media_id=response.json().get("id"))

    async def _post(
        self, app: AppIdentity, path: str, params: dict[str, str]
    ) -> tuple[httpx.Response, None] | tuple[None, PublishResult]:
        assert app.instagram is not None
        url = f"{self._settings.whatsapp_api_base_url}/{path}"
        headers = {"Authorization": f"Bearer {app.instagram.access_token}"}

        try:
            async with httpx.AsyncClient(timeout=self._settings.http_timeout_seconds) as client:
                response = await client.post(url, params=params, headers=headers)
        except httpx.HTTPError as exc:
            logger.warning("instagram.publish_failed", extra={"app_id": app.app_id, "error": str(exc)})
            return None, PublishResult(status=STATUS_FAILED, error=f"No se pudo contactar a Meta: {exc}")

        if response.is_error:
            detail = self._error_detail(response)
            logger.warning(
                "instagram.publish_rejected",
                extra={"app_id": app.app_id, "status_code": response.status_code, "detail": detail},
            )
            return None, PublishResult(status=STATUS_FAILED, error=detail)

        return response, None

    @staticmethod
    def _error_detail(response: httpx.Response) -> str:
        try:
            return str(response.json().get("error", {}).get("message", response.text))
        except ValueError:
            return response.text
