"""Publicar en Instagram.

Fuera de `/notifications` a proposito: eso manda mensajes a personas, esto
publica. No comparten destinatario, ni ventana de 24h, ni costo por
conversacion.

Ojo con lo que NO existe: los ESTADOS de WhatsApp no se pueden publicar por
API (Meta no expone endpoint y los marca "Not supported" en su tabla de
Coexistence). Solo Instagram.
"""
from __future__ import annotations

from fastapi import APIRouter, Depends
from pydantic import BaseModel, Field, model_validator

from nexolu_comms_api.core.auth.apps import AppIdentity
from nexolu_comms_api.core.auth.dependencies import get_current_app
from nexolu_comms_api.core.publishing.instagram import (
    STATUS_PUBLISHED,
    InstagramPublisher,
)

router = APIRouter(prefix="/v1", tags=["instagram"])


class StoryIn(BaseModel):
    """Una historia. Imagen O video, no los dos."""

    image_url: str | None = Field(
        default=None,
        description=(
            "URL PUBLICA de un JPEG (max 8 MB, 9:16). Meta la descarga al "
            "publicar, no al crear: una URL firmada que caduque entre los dos "
            "pasos falla en el segundo."
        ),
    )
    video_url: str | None = Field(
        default=None,
        description="URL publica de un MP4/MOV (3-60 s, max 100 MB, 9:16).",
    )
    mentions: list[str] = Field(
        default_factory=list,
        max_length=5,
        description=(
            "Cuentas a mencionar. Es lo UNICO que la API deja poner encima: "
            "no hay stickers, enlaces, encuestas, ubicacion ni musica."
        ),
    )

    @model_validator(mode="after")
    def _uno_u_otro(self) -> StoryIn:
        if bool(self.image_url) == bool(self.video_url):
            raise ValueError("Manda image_url o video_url, exactamente uno.")
        return self


class StoryOut(BaseModel):
    status: str
    media_id: str | None = None
    error: str | None = None


@router.post("/instagram/stories", response_model=StoryOut)
async def publish_story(
    payload: StoryIn,
    app: AppIdentity = Depends(get_current_app),
) -> StoryOut:
    """Publica una historia en la cuenta de Instagram de la app que llama.

    Devuelve 200 con `status` aunque falle, igual que el envio de mensajes:
    una historia rechazada por Meta no es un error de ESTA API, y quien
    llama necesita el motivo para decidir si reintenta o corrige el medio.
    """
    resultado = await InstagramPublisher().publish_story(
        app,
        image_url=payload.image_url,
        video_url=payload.video_url,
        mentions=payload.mentions,
    )

    return StoryOut(
        status=resultado.status,
        media_id=resultado.media_id if resultado.status == STATUS_PUBLISHED else None,
        error=resultado.error,
    )
