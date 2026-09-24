"""Dependencias de FastAPI para autenticar aplicaciones cliente.

Este servicio no tiene sesion de usuario final: su unico sujeto autenticado
es la APLICACION que llama (POS, Spa, EasyTickets), via API key en el header
`Authorization`. A quien se le envia el mensaje viaja en el body de cada
request, y se confia en el precisamente porque la llamada completa ya esta
autenticada por la API key de la app.

Hay un segundo nivel de auth, separado: la API key de PLATAFORMA (Nexolu, no
una app individual), que solo protege endpoints de reporte cross-app (ver
`require_platform_access` y GET /v1/platform/usage). Ninguna app integradora
la conoce. Mismo patron que Nexolu IA Core.
"""
from __future__ import annotations

import hmac

from fastapi import Depends, Header, HTTPException, status
from sqlalchemy.ext.asyncio import AsyncSession

from nexolu_comms_api.config import get_settings
from nexolu_comms_api.core.auth.apps import AppIdentity, resolve_by_api_key
from nexolu_comms_api.core.auth.panel import (
    UNRESTRICTED_SCOPE,
    PanelIdentity,
    PanelScope,
    embed_scope_from_token,
    resolve_identity_by_token,
)
from nexolu_comms_api.core.db.session import get_session


async def get_current_app(
    authorization: str | None = Header(default=None),
    session: AsyncSession = Depends(get_session),
) -> AppIdentity:
    if not authorization or not authorization.lower().startswith("bearer "):
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Falta el header Authorization.")

    api_key = authorization.split(" ", 1)[1].strip()
    app = await resolve_by_api_key(session, api_key)

    if app is None:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="API key invalida.")

    return app


def _bearer_credential(authorization: str | None) -> str:
    if not authorization or not authorization.lower().startswith("bearer "):
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Falta el header Authorization.")
    return authorization.split(" ", 1)[1].strip()


def _is_platform_key(credential: str) -> bool:
    settings = get_settings()

    if not settings.nexolu_platform_api_key:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="El acceso de plataforma no esta configurado (falta NEXOLU_PLATFORM_API_KEY).",
        )

    # Comparacion de tiempo constante: esta key da acceso a costos de TODAS
    # las apps, vale la pena cerrar el timing side-channel aunque el resto
    # del servicio no lo haga sistematicamente.
    return hmac.compare_digest(credential, settings.nexolu_platform_api_key)


async def require_platform_access(
    authorization: str | None = Header(default=None),
    session: AsyncSession = Depends(get_session),
) -> None:
    """Acceso TOTAL de plataforma. Dos credenciales validas: la platform
    key (servicios server-side: el BFF de nexolu-admin) o el JWT de un
    usuario del panel con rol `platform` (el admin de Nexolu en
    connect.nexolu.co). Un JWT de rol `client` NO pasa por aca - los
    clientes externos consumen los endpoints con scoping via
    `get_panel_scope`, nunca los de plataforma completa."""
    credential = _bearer_credential(authorization)

    if _is_platform_key(credential):
        return

    identity = await resolve_identity_by_token(session, credential)
    if identity is not None and identity.is_platform:
        return

    raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="API key de plataforma invalida.")


async def get_chat_scope(
    authorization: str | None = Header(default=None),
    session: AsyncSession = Depends(get_session),
) -> PanelScope:
    """Alcance del que llama, para lo que hace falta para ATENDER el chat
    (la bandeja, respuestas rapidas, plantillas para enviar, adjuntos).

    - platform key o usuario `platform` -> sin restriccion (app_ids=None).
    - usuario `client` -> solo las apps de sus membresias, y si alguna es
      de un negocio, solo ese negocio. La lista puede estar vacia (usuario
      recien creado sin membresias): eso es "no ve nada", no "ve todo" -
      fallar cerrado.
    - token de bandeja embebida -> UN negocio de UNA app.

    El filtro se aplica SIEMPRE del lado del servidor con `PanelScope`:
    que el front pida otra app no importa, la respuesta viene recortada.
    """
    credential = _bearer_credential(authorization)

    if _is_platform_key(credential):
        return UNRESTRICTED_SCOPE

    # La bandeja embebida en el panel de otra app: no es una persona, es
    # UN negocio dentro de UNA app mirando lo suyo. Se prueba antes que la
    # sesion de panel porque llega por el mismo header, y devuelve el
    # alcance mas estrecho que existe aca.
    embed = embed_scope_from_token(credential)
    if embed is not None:
        return embed

    identity = await resolve_identity_by_token(session, credential)
    if identity is None:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Sesion invalida o expirada.")
    return identity.scope


async def get_panel_scope(
    scope: PanelScope = Depends(get_chat_scope),
) -> PanelScope:
    """Alcance para ADMINISTRAR una app (flujos, plantillas, canales,
    credenciales, webhooks, uso): igual que `get_chat_scope`, pero quien
    solo ve UN negocio no entra.

    Esas tablas son de la app entera -- los flujos del Spa son los de
    todos los salones -- y recortarlas por negocio no tiene sentido; la
    recepcionista de un salon (o la bandeja embebida) vino a contestar
    mensajes, no a editar el bot de todos. 404 y no 403, como el resto
    del scoping: no se confirma que exista lo que no puede ver.
    """
    if scope.is_business_restricted:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="No encontrado.")
    return scope


async def get_panel_identity(
    authorization: str | None = Header(default=None),
    session: AsyncSession = Depends(get_session),
) -> PanelIdentity:
    """Una PERSONA del panel (no la platform key ni la bandeja embebida):
    para lo que es de alguien, como los avisos a su celular."""
    credential = _bearer_credential(authorization)
    identity = await resolve_identity_by_token(session, credential)
    if identity is None:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Sesion invalida o expirada.")
    return identity


def require_scope_for_app(scope: PanelScope, app_id: str) -> None:
    """404 y no 403 a proposito: a un cliente externo no se le confirma
    que la app ajena exista - misma respuesta que si no existiera."""
    if not scope.allows(app_id):
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="App desconocida.")
