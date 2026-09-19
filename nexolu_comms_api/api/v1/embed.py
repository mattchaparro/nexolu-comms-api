"""La bandeja de Connect, vista desde el panel de la app duena.

Por que existe. La bandeja de WhatsApp se escribio dos veces: una en
Connect y otra, mas pobre, dentro del Spa. Cada mejora -- buscar,
plantillas, adjuntos, la ficha del contacto, las respuestas rapidas --
habia que hacerla dos veces o dejar una de las dos atras. Con dos apps ya
duele; con cinco no se sostiene.

Asi que se hace una sola vez, en Connect, y el panel del Spa la muestra
adentro. Para eso hace falta una credencial que el navegador del salon
pueda llevar, y que NO sea la API key del Spa: esa es de servidor y no
puede bajar a un navegador nunca.

Este endpoint la emite. Lo llama el Spa de servidor a servidor con su API
key, diciendo de que negocio es la pantalla que va a pintar, y devuelve
un token corto que solo sirve para el chat de ESE negocio. Quien decide
que Ana puede ver la bandeja de Luxury Nails es el Spa, que es donde Ana
tiene su cuenta y sus permisos; Connect no conoce a Ana y no tiene por
que.
"""
from __future__ import annotations

from datetime import UTC, datetime, timedelta

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel, Field

from nexolu_comms_api.core.auth.apps import AppIdentity
from nexolu_comms_api.core.auth.dependencies import get_current_app
from nexolu_comms_api.core.security.panel import create_embed_token

router = APIRouter(prefix="/v1/embed", tags=["embed"])

# Quince minutos. El token viaja en la URL de un iframe -- historial del
# navegador, `Referer` -- que es el peor sitio donde puede estar una
# credencial. El panel que lo embebe pide otro cuando este caduca, y esa
# renovacion no le cuesta nada a nadie.
TTL_MINUTOS = 15


class EmbedTokenIn(BaseModel):
    business_id: str = Field(min_length=1, max_length=64)


class EmbedTokenOut(BaseModel):
    token: str
    expires_at: datetime
    # La app no tiene por que saberse la ruta del panel de Connect: se la
    # damos hecha para que un cambio de ruta no obligue a desplegar las
    # otras apps.
    url: str


@router.post("/chat-token", response_model=EmbedTokenOut)
async def mint_chat_token(
    payload: EmbedTokenIn,
    app: AppIdentity = Depends(get_current_app),
) -> EmbedTokenOut:
    """Un token de quince minutos para pintar la bandeja de un negocio."""
    from nexolu_comms_api.config import get_settings

    settings = get_settings()

    if not settings.panel_jwt_secret:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="El panel no esta configurado (falta PANEL_JWT_SECRET).",
        )

    if not settings.panel_base_url:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Falta PANEL_BASE_URL: sin ella no se sabe que pagina embeber.",
        )

    token = create_embed_token(app.app_id, payload.business_id, TTL_MINUTOS)

    return EmbedTokenOut(
        token=token,
        expires_at=datetime.now(UTC) + timedelta(minutes=TTL_MINUTOS),
        url=f"{settings.panel_base_url.rstrip('/')}/embebido/chat",
    )
