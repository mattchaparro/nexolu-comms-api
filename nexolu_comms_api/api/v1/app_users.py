"""Las personas de otra app que atienden el chat EN Connect.

Por que existe. La bandeja se escribe una sola vez, aca, y el Spa ya no
tiene la suya: su menu "WhatsApp" abre Connect. Pero quien entra no es
el admin de Nexolu sino la recepcionista de UN salon, que tiene su
cuenta y sus permisos en el Spa, no aca. Connect no le pide contrasena:
le cree a su app.

El Spa llama de servidor a servidor con su API key y dice quien es la
persona (`user_ref`, su id alla) y de que salon. Connect crea o pone al
dia a ese usuario -- `client`, con membresia de ESE negocio y nada mas --
y devuelve un pase de un solo uso para entrar. Quien decide que Ana puede
ver los chats de Luxury Nails es el Spa; Connect solo lo recorta.

La API key del Spa NUNCA baja a un navegador: por eso el pase lo pide el
servidor del Spa y al navegador solo le llega la URL con el pase.
"""
from __future__ import annotations

import hashlib
import re
import secrets
from datetime import UTC, datetime, timedelta
from urllib.parse import urlencode

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel, Field
from sqlalchemy import delete
from sqlalchemy.ext.asyncio import AsyncSession

from nexolu_comms_api.config import get_settings
from nexolu_comms_api.core.auth.apps import AppIdentity
from nexolu_comms_api.core.auth.dependencies import get_current_app
from nexolu_comms_api.core.auth.panel import ROLE_CLIENT, PanelScope, PanelUserRepository
from nexolu_comms_api.core.db.entities import PanelMembership, PanelUser, PushSubscription
from nexolu_comms_api.core.db.session import get_session

router = APIRouter(prefix="/v1/app-users", tags=["app-users"])

# Dos minutos: lo que tarda un celular lento en abrir otra pestana. El
# pase se quema al usarlo, asi que la ventana solo importa si se filtra
# sin usarse.
TICKET_TTL_SECONDS = 120

_REF = re.compile(r"^[A-Za-z0-9_-]{1,64}$")


def synthetic_email(app_id: str, user_ref: str) -> str:
    """El email de un usuario que viene de otra app. No es un correo: es
    su identidad aca, unica por (app, id alla), y a proposito no puede
    coincidir con el correo real de nadie -- asi el enlace del Spa nunca
    entra a alguien como el admin `platform` que tambien es."""
    return f"{user_ref}@{app_id}.apps.connect".lower()


def _check_ref(user_ref: str) -> str:
    if not _REF.match(user_ref):
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="user_ref solo admite letras, numeros, _ y - (max 64).",
        )
    return user_ref


class LoginTicketIn(BaseModel):
    user_ref: str = Field(min_length=1, max_length=64)
    business_id: str = Field(min_length=1, max_length=64)
    full_name: str = Field(default="", max_length=128)
    # A donde llevarla despues de entrar (ruta del panel, p.ej.
    # "/chat?c=<contact_id>"). Solo rutas locales.
    next: str = Field(default="/chat", max_length=256)


class LoginTicketOut(BaseModel):
    url: str
    expires_at: datetime


class UnreadOut(BaseModel):
    unread: int


@router.post("/login-ticket", response_model=LoginTicketOut)
async def issue_login_ticket(
    payload: LoginTicketIn,
    app: AppIdentity = Depends(get_current_app),
    session: AsyncSession = Depends(get_session),
) -> LoginTicketOut:
    """Crea o pone al dia a la persona y le da un pase para entrar."""
    settings = get_settings()
    if not settings.panel_base_url:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Falta PANEL_BASE_URL: sin ella no se sabe a donde mandar a la persona.",
        )

    user_ref = _check_ref(payload.user_ref)
    target = payload.next if payload.next.startswith("/") and not payload.next.startswith("//") else "/chat"

    email = synthetic_email(app.app_id, user_ref)
    user = await PanelUserRepository(session).get_by_email(email)
    if user is None:
        user = PanelUser(email=email, role=ROLE_CLIENT, origin_app_id=app.app_id, origin_user_ref=user_ref)
        user.memberships = []
        session.add(user)

    # La app manda: cada entrada re-sincroniza nombre, salon y que este
    # activa. Si la cambiaron de salon alla, aca deja de ver el anterior.
    user.full_name = payload.full_name.strip() or user.full_name or user_ref
    user.is_active = True
    user.role = ROLE_CLIENT
    wanted = (app.app_id, payload.business_id)
    if [(m.app_id, m.business_id) for m in user.memberships] != [wanted]:
        user.memberships.clear()
        await session.flush()
        user.memberships.append(PanelMembership(app_id=app.app_id, business_id=payload.business_id))

    ticket = secrets.token_urlsafe(32)
    user.login_ticket_hash = hashlib.sha256(ticket.encode("utf-8")).hexdigest()
    user.login_ticket_expires_at = datetime.utcnow() + timedelta(seconds=TICKET_TTL_SECONDS)
    await session.commit()

    # En el fragmento (#): el navegador no lo manda al servidor, asi que no
    # queda en los logs de nginx ni en un Referer.
    fragment = urlencode({"ticket": ticket, "next": target})
    return LoginTicketOut(
        url=f"{settings.panel_base_url.rstrip('/')}/entrar#{fragment}",
        expires_at=datetime.now(UTC) + timedelta(seconds=TICKET_TTL_SECONDS),
    )


@router.delete("/{user_ref}", status_code=status.HTTP_204_NO_CONTENT)
async def deactivate_app_user(
    user_ref: str,
    app: AppIdentity = Depends(get_current_app),
    session: AsyncSession = Depends(get_session),
) -> None:
    """La app dice que esta persona ya no atiende el chat (la
    desactivaron o le quitaron el permiso). Su sesion muere en la
    siguiente peticion -- la identidad se resuelve contra la BD en cada
    una -- y su celular deja de recibir avisos ya."""
    email = synthetic_email(app.app_id, _check_ref(user_ref))
    user = await PanelUserRepository(session).get_by_email(email)
    if user is not None:
        user.is_active = False
        user.login_ticket_hash = None
        user.login_ticket_expires_at = None
    await session.execute(delete(PushSubscription).where(PushSubscription.user_email == email))
    await session.commit()


@router.get("/unread", response_model=UnreadOut)
async def unread_count(
    business_id: str,
    app: AppIdentity = Depends(get_current_app),
    session: AsyncSession = Depends(get_session),
) -> UnreadOut:
    """Cuantas conversaciones de ese negocio esperan respuesta: el numerito
    del menu "WhatsApp" de la app, ahora que la bandeja vive aca."""
    from nexolu_comms_api.api.v1.admin_chats import list_conversations

    scope = PanelScope(app_ids=(app.app_id,), business_ids=(business_id,))
    result = await list_conversations(
        app_id=None, q=None, only_unread=True, limit=1, offset=0, scope=scope, session=session
    )
    return UnreadOut(unread=result.unread_total)
