"""Schemas del panel administrativo (apps + credenciales de proveedor).

Separados de los schemas de la API publica (definidos inline en
api/v1/notifications.py, usage.py, whatsapp.py) porque tienen una audiencia
y un ciclo de vida distintos: estos los consume unicamente el Admin General
via `require_platform_access`, nunca una app integradora.
"""
from __future__ import annotations

from datetime import datetime

from pydantic import BaseModel, Field


class CommsAppIn(BaseModel):
    """Payload de POST /v1/admin/apps."""

    app_id: str = Field(min_length=1, max_length=64, pattern=r"^[a-z0-9][a-z0-9_-]*$")
    name: str = ""


class CommsAppPatch(BaseModel):
    """Payload de PATCH /v1/admin/apps/{app_id}. Todo opcional: solo se
    actualiza lo que venga distinto de None."""

    name: str | None = None
    is_active: bool | None = None


class CommsAppOut(BaseModel):
    """Una app tal como la ve el admin - la key SIEMPRE enmascarada, nunca
    se vuelve a mostrar en claro despues de crearla/regenerarla."""

    id: str
    app_id: str
    name: str
    api_key_masked: str
    is_active: bool
    has_meta_whatsapp: bool = False
    has_brevo: bool = False
    created_at: datetime
    updated_at: datetime


class CommsAppCreatedOut(CommsAppOut):
    """Solo la respuesta de creacion/regeneracion trae la key en claro."""

    api_key: str


# -- Meta WhatsApp Cloud API -------------------------------------------------


class MetaWhatsAppIn(BaseModel):
    """Payload de POST .../providers/meta-whatsapp. Upsert: crea la
    credencial si no existe, la sobreescribe por completo si ya existe -
    "rotar" un access token es pegar el que el operador ya genero en Meta
    Business Manager (Meta no expone una API para generarlo)."""

    phone_number_id: str
    access_token: str
    waba_id: str | None = None
    webhook_verify_token: str | None = None
    meta_app_secret: str | None = None
    callback_secret: str | None = None
    callback_url: str | None = None


class MetaWhatsAppStatusOut(BaseModel):
    """Solo campos no secretos - nunca incluye access_token/meta_app_secret/
    callback_secret/webhook_verify_token."""

    configured: bool
    phone_number_id: str | None = None
    waba_id: str | None = None
    callback_url: str | None = None


class MetaInstagramIn(BaseModel):
    """Payload de POST .../providers/meta-instagram.

    El token NO es el de WhatsApp aunque el negocio sea el mismo: publicar
    en Instagram usa otro flujo de login y otros permisos
    (`instagram_business_content_publish`). Y CADUCA: Meta emite tokens de
    60 dias renovables, sin equivalente al token permanente de usuario del
    sistema que si existe para WhatsApp. Si nadie lo renueva, publicar deja
    de funcionar sin aviso.
    """

    ig_user_id: str
    access_token: str
    username: str | None = None


class MetaInstagramStatusOut(BaseModel):
    """Solo campos no secretos - nunca incluye access_token."""

    configured: bool
    ig_user_id: str | None = None
    username: str | None = None


class MetaInstagramSecretsOut(BaseModel):
    """Reveal bajo demanda - endpoint separado de status, nunca inline."""

    access_token: str


class MetaWhatsAppSecretsOut(BaseModel):
    """Reveal bajo demanda - endpoint separado de status, nunca inline."""

    access_token: str
    webhook_verify_token: str | None = None
    meta_app_secret: str | None = None
    callback_secret: str | None = None


# -- Brevo --------------------------------------------------------------------


class BrevoIn(BaseModel):
    """Payload de POST .../providers/brevo. Igual que Meta: rotar la key es
    pegar la nueva que el operador ya genero en el dashboard de Brevo."""

    from_email: str
    from_name: str = ""
    brevo_api_key: str


class BrevoStatusOut(BaseModel):
    configured: bool
    from_email: str | None = None
    from_name: str | None = None


class BrevoSecretsOut(BaseModel):
    brevo_api_key: str
