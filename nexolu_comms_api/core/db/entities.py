"""Modelos de persistencia del servicio.

Deliberadamente NO hay tabla de negocio aca (sin `productos`, sin `ventas`):
eso vive en la base de datos de cada aplicacion. Lo que este servicio
persiste es el rastro de cada envio (auditoria + la fuente cruda para los
reportes de uso/costo) - el estado que le pertenece al envio de mensajes, no
al negocio.
"""
from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any

from sqlalchemy import JSON, Boolean, DateTime, ForeignKey, Index, Integer, String, Text, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column, relationship

from nexolu_comms_api.core.db.session import Base
from nexolu_comms_api.core.security.api_keys import generate_api_key, hash_api_key
from nexolu_comms_api.core.security.crypto import EncryptedJSON, EncryptedString


def _uuid() -> str:
    return uuid.uuid4().hex


class CommsApp(Base):
    """Aplicacion cliente autorizada a consumir este servicio (POS, Spa,
    EasyTickets...), persistida en BD - reemplaza el registro en memoria que
    hoy vive en `NEXOLU_APPS_JSON` (ver `core/auth/apps.py` para el periodo
    de transicion con fallback).

    `api_key` se guarda cifrada (reversible) porque en teoria podria hacer
    falta reenviarla; `api_key_hash` (SHA-256) es lo que autentica la llamada
    ENTRANTE en tiempo constante, sin descifrar nada en el camino caliente.
    Mismo patron que `AppRegistration` en nexolu-ia-core / `Integration` en
    nexolu-payments-core."""

    __tablename__ = "comms_apps"

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=_uuid)
    app_id: Mapped[str] = mapped_column(String(64), unique=True)
    name: Mapped[str] = mapped_column(String(128), default="")
    api_key: Mapped[str] = mapped_column(EncryptedString(255), nullable=False)
    api_key_hash: Mapped[str] = mapped_column(String(64), unique=True, nullable=False)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)

    provider_credentials: Mapped[list[ProviderCredential]] = relationship(back_populates="app")

    def __init__(self, **kwargs):
        api_key = kwargs.pop("api_key", None) or generate_api_key()
        self.api_key = api_key
        self.api_key_hash = hash_api_key(api_key)
        super().__init__(**kwargs)


class ProviderCredential(Base):
    """Credenciales de UN proveedor (`provider_slug` = "meta_whatsapp" |
    "brevo") para UNA `CommsApp`. Tabla separada (no columnas fijas en
    `CommsApp`) porque cada app puede tener 0, 1 o 2 proveedores
    configurados de forma independiente - mismo patron que
    `ProviderCredential` en nexolu-payments-core (alli generalizado por
    `(merchant_id, provider_slug, environment)`; aca no hace falta
    `environment` porque ni Meta ni Brevo tienen un concepto real de
    sandbox/produccion para estas credenciales).

    `config`: campos NO secretos de ese proveedor (ids, urls, nombres de
    remitente) - se devuelven tal cual en el status/list del admin.
    `secrets`: campos SI secretos (access tokens, api keys, app secrets) -
    cifrados como un solo blob JSON (`EncryptedJSON`), solo se devuelven en
    el endpoint de "reveal" explicito, nunca en status/list."""

    __tablename__ = "provider_credentials"
    __table_args__ = (UniqueConstraint("app_id", "provider_slug", name="uq_provider_credential_app_provider"),)

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=_uuid)
    app_id: Mapped[str] = mapped_column(ForeignKey("comms_apps.id"), index=True)
    provider_slug: Mapped[str] = mapped_column(String(32))
    config: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    secrets: Mapped[dict[str, Any]] = mapped_column(EncryptedJSON(4000), default=dict)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)

    app: Mapped[CommsApp] = relationship(back_populates="provider_credentials")


class Notification(Base):
    """Un intento de envio por UN canal. Un POST /v1/notifications/send con
    3 canales genera 3 filas - cada canal se registra, factura y falla de
    forma independiente."""

    __tablename__ = "notifications"
    __table_args__ = (
        Index("ix_notifications_tenant", "app_id", "business_id"),
        Index("ix_notifications_reference", "reference"),
        Index("ix_notifications_created_at", "created_at"),
    )

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=_uuid)
    app_id: Mapped[str] = mapped_column(String(64))
    business_id: Mapped[str] = mapped_column(String(64))
    # Identificador libre que la app llamante define para agrupar/rastrear
    # (p.ej. "low_stock_alert:456") - este servicio no le da significado,
    # solo lo guarda y lo permite filtrar.
    reference: Mapped[str | None] = mapped_column(String(191), nullable=True)
    channel: Mapped[str] = mapped_column(String(32))
    recipient: Mapped[str] = mapped_column(String(255))
    status: Mapped[str] = mapped_column(String(16))  # sent | failed | skipped
    provider_message_id: Mapped[str | None] = mapped_column(String(191), nullable=True)
    error: Mapped[str | None] = mapped_column(Text, nullable=True)
    # None cuando el proveedor no informa costo para ese envio (p.ej. email
    # via Brevo) - no es lo mismo que "cost=0". Ver core/telemetry/usage.py.
    cost_micros: Mapped[int | None] = mapped_column(Integer, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)
