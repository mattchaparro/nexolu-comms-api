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

from sqlalchemy import (
    JSON,
    Boolean,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
)
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


class WebhookEvent(Base):
    """Un evento entrante de Meta, persistido ANTES de responderle 200.

    Existe porque el reenvio al `callback_url` de la app puede fallar (app
    caida, deploy a medias, timeout) y hasta esta tabla el evento vivia solo
    en memoria del BackgroundTask: si el reenvio fallaba, el evento se
    perdia - y un webhook `order` perdido es una venta perdida. Ahora el
    evento crudo queda en BD y un worker lo reintenta con backoff (ver
    core/webhooks/forwarder.py) hasta entregarlo o declararlo `dead`.

    `payload` es el CUERPO CRUDO tal cual llego (utf-8): la firma HMAC del
    reenvio se calcula sobre esos bytes exactos, igual que el reenvio
    inmediato de antes - re-serializar JSON cambiaria la firma.

    `event_type`/`phone_number_id` son METADATOS para el panel (filtrar
    "pedidos fallidos", enrutar por numero), no interpretacion de negocio:
    el contenido sigue viajando intacto a la app duena.
    """

    __tablename__ = "webhook_events"
    __table_args__ = (
        Index("ix_webhook_events_retry", "forward_status", "next_retry_at"),
        Index("ix_webhook_events_app", "app_id", "received_at"),
    )

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=_uuid)
    app_id: Mapped[str] = mapped_column(String(64))
    # Presente solo para eventos que entraron por el webhook de PLATAFORMA
    # (numero propio de un negocio via Embedded Signup): enlaza el evento
    # con su BusinessChannel para reenviar con el business_id resuelto.
    business_channel_id: Mapped[str | None] = mapped_column(String(32), nullable=True)
    event_type: Mapped[str] = mapped_column(String(64), default="unknown")
    phone_number_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    payload: Mapped[str] = mapped_column(Text)
    # None = no se pudo verificar (la app no tiene meta_app_secret
    # configurado); True/False = verificada con el secret.
    signature_valid: Mapped[bool | None] = mapped_column(Boolean, nullable=True)
    # Solo para event_type=message: True/False = el motor de flujos SI/NO
    # atendio este mensaje (avanzo una sesion o arranco un flujo). Viaja a
    # la app duena como X-Nexolu-Flow-Handled para que su bot calle cuando
    # un flujo ya respondio. None = el motor no alcanzo a pronunciarse.
    flow_handled: Mapped[bool | None] = mapped_column(Boolean, nullable=True)
    # pending: persistido, aun sin intento | delivered: la app respondio 2xx
    # failed: fallo, hay reintento programado (next_retry_at)
    # dead: se agotaron los reintentos | skipped: app sin callback_url
    # rejected: firma de Meta invalida - nunca se reenvia
    forward_status: Mapped[str] = mapped_column(String(16), default="pending")
    attempts: Mapped[int] = mapped_column(Integer, default=0)
    next_retry_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    last_error: Mapped[str | None] = mapped_column(Text, nullable=True)
    received_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)
    delivered_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)


class PanelUser(Base):
    """Usuario del panel Connect (connect.nexolu.co).

    Dos roles con distincion dura (pedido explicito de Alejandro):
    `platform` es el admin de Nexolu (ve todas las apps: pos, spa, sga y
    los clientes externos); `client` es un negocio EXTERNO que usa Connect
    como producto y solo ve las apps a las que lo ata `PanelMembership`.

    `password_hash` (bcrypt) es opcional: un usuario sin el solo puede
    entrar por SSO (auth.nexolu.co). El operador de emergencia
    (PANEL_EMAIL/PANEL_PASSWORD_HASH en env) NO vive en esta tabla a
    proposito - es el break-glass que funciona aunque la BD este vacia,
    mismo patron que nexolu-admin."""

    __tablename__ = "panel_users"

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=_uuid)
    email: Mapped[str] = mapped_column(String(191), unique=True)
    full_name: Mapped[str] = mapped_column(String(128), default="")
    role: Mapped[str] = mapped_column(String(16), default="client")  # platform | client
    password_hash: Mapped[str | None] = mapped_column(String(128), nullable=True)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)
    last_login_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    # Usuario que viene de OTRA app (la recepcionista del Spa): no tiene
    # contrasena ni SSO, entra solo con el pase de un solo uso que pide su
    # app de servidor a servidor (api/v1/app_users.py). Su `email` es
    # sintetico ({ref}@{app}.apps.connect) a proposito: la misma persona
    # puede ser admin `platform` con su correo real, y el enlace del Spa
    # NO debe entrarla como admin.
    origin_app_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    origin_user_ref: Mapped[str | None] = mapped_column(String(64), nullable=True)
    # El pase vigente, hasheado (sha256): sirve una vez y por segundos.
    login_ticket_hash: Mapped[str | None] = mapped_column(String(64), nullable=True, index=True)
    login_ticket_expires_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)

    memberships: Mapped[list[PanelMembership]] = relationship(
        back_populates="user", cascade="all, delete-orphan"
    )


class PanelMembership(Base):
    """A que app (negocio) puede entrar un usuario `client`. Un cliente
    externo ES una `CommsApp` (con sus credenciales, canales y uso, igual
    que pos/spa) - la membresia solo lo ata a ella. `app_id` es el string
    publico de la app, no el PK interno, por la misma razon que en
    `Notification`/`BusinessChannel`: sobrevive al fallback legado y se
    filtra directo contra esas tablas."""

    __tablename__ = "panel_memberships"
    __table_args__ = (UniqueConstraint("user_id", "app_id", name="uq_panel_membership_user_app"),)

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=_uuid)
    user_id: Mapped[str] = mapped_column(ForeignKey("panel_users.id"), index=True)
    app_id: Mapped[str] = mapped_column(String(64))
    # "" = toda la app (el cliente externo dueno de su CommsApp). Con valor
    # = solo ESE negocio de la app: "la app spa" son todos los salones, y
    # la recepcionista de uno no puede ver los chats de otro.
    business_id: Mapped[str] = mapped_column(String(64), default="")
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)

    user: Mapped[PanelUser] = relationship(back_populates="memberships")


class PushSubscription(Base):
    """Un navegador (casi siempre un celular) que pidio que le avisen
    cuando alguien escribe. Es de una PERSONA del panel -- `user_email` es
    el mismo `sub` de su sesion -- y a quien se le manda se decide al
    momento de enviar con su alcance vigente (core/push.py): quitarle un
    negocio a alguien le corta los avisos de ese negocio sin tocar esta
    tabla.

    `endpoint` es unico: el navegador lo da por suscripcion, y si otra
    persona entra en el mismo celular, la fila pasa a ser de ella."""

    __tablename__ = "push_subscriptions"

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=_uuid)
    user_email: Mapped[str] = mapped_column(String(191), index=True)
    endpoint: Mapped[str] = mapped_column(String(512), unique=True)
    p256dh: Mapped[str] = mapped_column(String(255))
    auth: Mapped[str] = mapped_column(String(64))
    content_encoding: Mapped[str] = mapped_column(String(16), default="aes128gcm")
    user_agent: Mapped[str] = mapped_column(String(255), default="")
    last_sent_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)


class BusinessChannel(Base):
    """Identidad de WhatsApp de UN negocio dentro de una app: su propia
    WABA, su propio numero y su propio token, obtenidos via Embedded Signup
    (ver api/v1/onboarding.py).

    Convive con `ProviderCredential`: esa sigue siendo el numero COMPARTIDO
    de la app (el POS multi-tenant de hoy), esta es el numero PROPIO de un
    negocio. El envio resuelve primero por (app_id, business_id) aca y cae
    a la credencial de la app si no hay canal propio - asi los negocios
    migran a numero propio uno a uno sin romper a los demas.

    `app_id`/`business_id` son los mismos strings opacos de `Notification`:
    este servicio no valida el business_id contra nada propio, es la app
    duena quien le da significado (igual que en el resto del servicio).

    `status`: pending (signup iniciado, sin completar) | active |
    disconnected (token revocado desde Meta Business Suite, o desconexion
    manual desde el panel - se conserva la fila para reconectar y para que
    el historial de webhooks/notificaciones no quede huerfano).
    """

    __tablename__ = "business_channels"
    __table_args__ = (
        UniqueConstraint("app_id", "business_id", name="uq_business_channel_app_business"),
        Index("ix_business_channels_phone", "phone_number_id"),
    )

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=_uuid)
    app_id: Mapped[str] = mapped_column(String(64))
    business_id: Mapped[str] = mapped_column(String(64))
    waba_id: Mapped[str] = mapped_column(String(64))
    phone_number_id: Mapped[str] = mapped_column(String(64))
    display_phone_number: Mapped[str | None] = mapped_column(String(32), nullable=True)
    # Business integration system user access token, del cliente onboardeado
    # (ver el analisis, seccion I). Cifrado en reposo como todo secreto.
    access_token: Mapped[str] = mapped_column(EncryptedString(1024))
    # PIN de verificacion en dos pasos registrado por este servicio: hace
    # falta de nuevo para re-registrar o migrar el numero.
    pin: Mapped[str | None] = mapped_column(EncryptedString(255), nullable=True)
    catalog_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    status: Mapped[str] = mapped_column(String(16), default="active")
    last_error: Mapped[str | None] = mapped_column(Text, nullable=True)
    connected_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    disconnected_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)


class WhatsAppTemplate(Base):
    """Espejo local de UNA plantilla de mensaje de Meta.

    Meta es la fuente de verdad del ESTADO (el aprobado/rechazado lo decide
    su revision y llega por el webhook `message_template_status_update` o
    por un sync manual); este espejo existe para que (1) el panel liste y
    cree plantillas sin pegarle a Graph API en cada carga, y (2) el envio
    pueda avisar ANTES de llamar a Meta que una plantilla no esta aprobada
    - un envio masivo con una plantilla PAUSED que falla mensaje a mensaje
    es plata y tiempo perdidos.

    La identidad natural de Meta es (waba_id, name, language) - por eso la
    restriccion unica es esa y no el `meta_template_id` (que llega despues,
    con la respuesta de creacion o el primer sync).

    `business_channel_id`: NULL = plantilla de la WABA compartida de la
    app; con valor = de la WABA propia de ese negocio (Embedded Signup).
    """

    __tablename__ = "whatsapp_templates"
    __table_args__ = (
        UniqueConstraint("waba_id", "name", "language", name="uq_whatsapp_template_identity"),
        Index("ix_whatsapp_templates_app", "app_id"),
    )

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=_uuid)
    app_id: Mapped[str] = mapped_column(String(64))
    business_channel_id: Mapped[str | None] = mapped_column(String(32), nullable=True)
    waba_id: Mapped[str] = mapped_column(String(64))
    name: Mapped[str] = mapped_column(String(191))
    language: Mapped[str] = mapped_column(String(16))
    category: Mapped[str] = mapped_column(String(32))  # MARKETING | UTILITY | AUTHENTICATION
    # PENDING | APPROVED | REJECTED | PAUSED | DISABLED | IN_APPEAL ... -
    # se guarda lo que Meta diga, sin lista cerrada: Meta agrega estados.
    status: Mapped[str] = mapped_column(String(32), default="PENDING")
    meta_template_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    components: Mapped[list[dict[str, Any]]] = mapped_column(JSON, default=list)
    quality_score: Mapped[str | None] = mapped_column(String(32), nullable=True)
    # Motivo del ultimo rechazo/pausa que reporto Meta.
    reason: Mapped[str | None] = mapped_column(Text, nullable=True)
    last_synced_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)


class WhatsAppFlow(Base):
    """Espejo local de UN WhatsApp Flow de Meta (en el panel: "Formulario").

    OJO con el nombre: NO es un `Flow` de este servicio (el motor tipo
    ManyChat de core/flows/engine.py). Este es el formulario nativo de
    WhatsApp que Meta pinta dentro del chat; en codigo se llama como Meta lo
    llama y en el panel "Formulario" para no confundirlos.

    Mismo reparto que las plantillas: Meta es la fuente de verdad del ESTADO
    (DRAFT -> PUBLISHED -> DEPRECATED, mas BLOCKED/THROTTLED si tuviera
    endpoint) y de la validacion; aca se guarda el JSON que se subio (Meta
    no lo devuelve en el listado - hay que descargarlo del asset) y los
    ultimos `validation_errors`, para que el panel liste y edite sin pegarle
    a Graph API en cada carga.

    Un Flow PUBLICADO ya no se puede editar (regla de Meta): cambiarlo es
    crear otro borrador. Por eso el `json` de una fila publicada es historia,
    no un borrador en curso.

    `business_channel_id`: NULL = Flow de la WABA compartida de la app; con
    valor = de la WABA propia de ese negocio (Embedded Signup), igual que
    `WhatsAppTemplate`.
    """

    __tablename__ = "whatsapp_flows"
    __table_args__ = (
        UniqueConstraint("waba_id", "name", name="uq_whatsapp_flow_identity"),
        Index("ix_whatsapp_flows_app", "app_id"),
    )

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=_uuid)
    app_id: Mapped[str] = mapped_column(String(64))
    business_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    business_channel_id: Mapped[str | None] = mapped_column(String(32), nullable=True)
    waba_id: Mapped[str] = mapped_column(String(64))
    meta_flow_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    name: Mapped[str] = mapped_column(String(191))
    categories: Mapped[list[str]] = mapped_column(JSON, default=list)
    # DRAFT | PUBLISHED | DEPRECATED | BLOCKED | THROTTLED - lo que Meta
    # diga, sin lista cerrada (igual que las plantillas).
    status: Mapped[str] = mapped_column(String(32), default="DRAFT")
    # El Flow JSON TAL COMO se subio a Meta, como texto y no como columna
    # JSON: MySQL reordena las claves de un JSON, y entonces el editor
    # mostraria el formulario desordenado y la linea de un validation_error
    # de Meta ya no apuntaria a nada. NULL si se sincronizo sin poder
    # descargar el asset.
    flow_json: Mapped[str | None] = mapped_column(Text, nullable=True)
    # "version" del Flow JSON ("7.2"): define que componentes valen.
    json_version: Mapped[str | None] = mapped_column(String(16), nullable=True)
    validation_errors: Mapped[list[dict[str, Any]]] = mapped_column(JSON, default=list)
    preview_url: Mapped[str | None] = mapped_column(Text, nullable=True)
    preview_expires_at: Mapped[str | None] = mapped_column(String(40), nullable=True)
    # Clave de la plantilla de la biblioteca de la que salio (p.ej.
    # "confirm_booking"), para no duplicarla al auto-provisionar.
    library_key: Mapped[str | None] = mapped_column(String(64), nullable=True)
    last_synced_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    published_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)


class Contact(Base):
    """Un contacto de WhatsApp de un negocio, con tags y campos libres - el
    modelo subscriber/tags/custom-fields de ManyChat, que es lo que permite
    intercambiar datos entre las apps y los flujos.

    `business_id` con "" = contacto a nivel de app (numero compartido; la
    app multi-tenant resuelve su negocio por su lado). Cadena vacia y no
    NULL a proposito: NULL no participa en restricciones unicas en MySQL/
    SQLite y la identidad (app, negocio, telefono) debe ser unica de
    verdad.

    `tags`: lista de strings. `fields`: dict libre (el significado lo dan
    las apps y los flujos, este servicio no lo interpreta - solo lo
    interpola en los mensajes de los flujos)."""

    __tablename__ = "contacts"
    __table_args__ = (
        UniqueConstraint("app_id", "business_id", "phone", name="uq_contact_identity"),
        Index("ix_contacts_app", "app_id", "business_id"),
    )

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=_uuid)
    app_id: Mapped[str] = mapped_column(String(64))
    business_id: Mapped[str] = mapped_column(String(64), default="")
    phone: Mapped[str] = mapped_column(String(32))
    name: Mapped[str] = mapped_column(String(128), default="")
    tags: Mapped[list[str]] = mapped_column(JSON, default=list)
    fields: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    last_inbound_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    # Estado de BANDEJA (no de contacto): hasta cuando alguien leyo este
    # hilo, y quien lo esta atendiendo. Vive aca y no en chat_messages
    # porque es por conversacion, no por mensaje - y la conversacion ES el
    # contacto. Sin "no leido" una clienta se queda sin respuesta y nadie
    # se entera; sin "quien atiende", dos personas contestan lo mismo.
    last_read_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    assigned_to: Mapped[str | None] = mapped_column(String(32), nullable=True)
    # Ultima vez que este hilo entro en un aviso de "sin responder". Evita
    # repetir el mismo aviso cada vuelta del worker: se vuelve a avisar solo
    # si llego algo NUEVO despues (ver core/alerts.py).
    alerted_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    # Lo que quien atiende necesita recordar de esta persona ("alergica al
    # acrilico", "siempre pide con Maria"). Vive en el contacto y no en un
    # mensaje porque no es parte de la conversacion: es lo que se sabe de
    # ella y hay que ver ANTES de contestar.
    notes: Mapped[str] = mapped_column(Text, default="")
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)


class ChatMessage(Base):
    """UN mensaje de la conversacion con un contacto - la bandeja/live
    chat de Connect (la de ManyChat). Critica operativamente: el numero
    del negocio puede no tener app movil ni SIM (Cloud API pura), asi que
    esta bandeja web es la UNICA forma humana de leer y responder.

    `direction`: in (del contacto, persistido por el webhook antes de
    cualquier logica de flujos) | out (del negocio o del motor de flujos).
    `body`: el texto legible (caption si fue multimedia); `message_type` y
    `payload` guardan el detalle para pintar burbujas ricas. `origin` de
    los salientes: panel | flow | api."""

    __tablename__ = "chat_messages"
    __table_args__ = (
        Index("ix_chat_messages_contact", "contact_id", "created_at"),
        Index("ix_chat_messages_app", "app_id", "business_id", "created_at"),
    )

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=_uuid)
    app_id: Mapped[str] = mapped_column(String(64))
    business_id: Mapped[str] = mapped_column(String(64), default="")
    contact_id: Mapped[str] = mapped_column(String(32))
    direction: Mapped[str] = mapped_column(String(8))  # in | out
    message_type: Mapped[str] = mapped_column(String(32), default="text")
    body: Mapped[str] = mapped_column(Text, default="")
    payload: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    wamid: Mapped[str | None] = mapped_column(String(191), nullable=True)
    status: Mapped[str] = mapped_column(String(16), default="")  # sent|failed|... (solo out)
    origin: Mapped[str] = mapped_column(String(16), default="")  # panel|flow|api (solo out)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)


class Flow(Base):
    """Un flujo de automatizacion de conversacion (el concepto central de
    ManyChat, adaptado al guardrail de Connect: el flujo orquesta la
    CONVERSACION - mensajes, botones, ramas, tags -; la accion de negocio
    real la ejecuta la app duena, via link web dentro del flujo o porque
    ella misma lo disparo por API).

    `trigger_type`: "keyword" (un mensaje entrante que matchee
    `trigger_keywords` lo arranca) o "api" (solo lo arranca la app via
    POST /v1/flows/trigger - el caso "agendaste una cita"). `definition`
    es el grafo de nodos - ver core/flows/engine.py para el esquema y su
    validacion."""

    __tablename__ = "flows"
    __table_args__ = (
        UniqueConstraint("app_id", "business_id", "name", name="uq_flow_identity"),
        Index("ix_flows_app", "app_id", "business_id"),
    )

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=_uuid)
    app_id: Mapped[str] = mapped_column(String(64))
    business_id: Mapped[str] = mapped_column(String(64), default="")
    name: Mapped[str] = mapped_column(String(128))
    trigger_type: Mapped[str] = mapped_column(String(16), default="api")  # keyword | api
    trigger_keywords: Mapped[list[str]] = mapped_column(JSON, default=list)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)
    definition: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)


class FlowSession(Base):
    """Donde va UN contacto dentro de UN flujo: el nodo en el que quedo
    esperando respuesta y el contexto de variables acumulado (las que trajo
    el trigger + las que fijan los nodos). Una sesion `active` por contacto
    como maximo (arrancar un flujo nuevo cierra la anterior como
    `superseded` - comportamiento ManyChat: el flujo mas reciente gana)."""

    __tablename__ = "flow_sessions"
    __table_args__ = (
        Index("ix_flow_sessions_contact", "contact_id", "status"),
        Index("ix_flow_sessions_resume", "status", "resume_at"),
    )

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=_uuid)
    flow_id: Mapped[str] = mapped_column(String(32))
    contact_id: Mapped[str] = mapped_column(String(32))
    app_id: Mapped[str] = mapped_column(String(64))
    business_id: Mapped[str] = mapped_column(String(64), default="")
    current_node: Mapped[str | None] = mapped_column(String(64), nullable=True)
    context: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    # active (esperando respuesta) | waiting (en un delay, ver resume_at)
    # | completed | superseded | expired
    status: Mapped[str] = mapped_column(String(16), default="active")
    # Solo con status=waiting: cuando el worker debe retomar en current_node.
    resume_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    started_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)


class CatalogItem(Base):
    """Estado de sincronizacion de UN producto contra el catalogo de Meta.

    La fuente de verdad del producto es la app duena (principio 45 del
    brief: el catalogo de Meta es una superficie comercial del catalogo de
    Nexolu). Esta tabla guarda que se mando, con que `content_hash` (para
    no re-enviar lo que no cambio: el rate limit oficial es ~100 llamadas
    de batch por hora por catalogo) y en que quedo: `pending` (batch
    enviado, con `batch_handle` para check_batch_request_status), `synced`,
    o `error` con el motivo que reporto Meta.

    `retailer_id` es EL identificador del producto en todo el circuito: el
    `id` del items_batch, el `product_retailer_id` del webhook `order` y el
    de los mensajes SPM/MPM."""

    __tablename__ = "catalog_items"
    __table_args__ = (
        UniqueConstraint("catalog_id", "retailer_id", name="uq_catalog_item_identity"),
        Index("ix_catalog_items_app", "app_id"),
    )

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=_uuid)
    app_id: Mapped[str] = mapped_column(String(64))
    business_channel_id: Mapped[str | None] = mapped_column(String(32), nullable=True)
    catalog_id: Mapped[str] = mapped_column(String(64))
    retailer_id: Mapped[str] = mapped_column(String(191))
    title: Mapped[str] = mapped_column(String(191), default="")
    description: Mapped[str] = mapped_column(Text, default="")
    # Formato oficial del batch: "<monto> <moneda>", p.ej. "9000 COP".
    price: Mapped[str] = mapped_column(String(32), default="")
    availability: Mapped[str] = mapped_column(String(16), default="in stock")  # in stock | out of stock
    image_link: Mapped[str | None] = mapped_column(String(512), nullable=True)
    link: Mapped[str | None] = mapped_column(String(512), nullable=True)
    brand: Mapped[str | None] = mapped_column(String(128), nullable=True)
    content_hash: Mapped[str] = mapped_column(String(64), default="")
    sync_status: Mapped[str] = mapped_column(String(16), default="pending")  # pending | synced | error
    batch_handle: Mapped[str | None] = mapped_column(String(255), nullable=True)
    last_error: Mapped[str | None] = mapped_column(Text, nullable=True)
    last_synced_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)


class IdempotencyRecord(Base):
    """Respuesta ya emitida para un `Idempotency-Key` de una app.

    `POST /v1/notifications/send` no era idempotente (limitacion conocida
    del README): un timeout del lado del caller + retry = mensaje doble al
    cliente final. Con esto, repetir la llamada con el mismo header
    `Idempotency-Key` devuelve la respuesta original sin volver a enviar
    nada. La clave la elige la app llamante (p.ej. su job id); este
    servicio no le da significado."""

    __tablename__ = "idempotency_records"
    __table_args__ = (UniqueConstraint("app_id", "idempotency_key", name="uq_idempotency_app_key"),)

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=_uuid)
    app_id: Mapped[str] = mapped_column(String(64))
    idempotency_key: Mapped[str] = mapped_column(String(191))
    response_body: Mapped[str] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)


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


class InboxAlertConfig(Base):
    """A quien se le avisa que hay conversaciones sin responder.

    Vive en Connect y no en la app dueña porque es una regla sobre la
    BANDEJA (que es de Connect), no sobre el negocio. Los avisos de negocio
    -- agendo, cancelo -- los manda la app, que es la que sabe de citas.

    Un solo config por (app, negocio). `business_id` "" = vale para toda la
    app (el numero compartido), misma convencion que Contact y Flow.
    """

    __tablename__ = "inbox_alert_configs"
    __table_args__ = (
        UniqueConstraint("app_id", "business_id", name="uq_inbox_alert_scope"),
    )

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=_uuid)
    app_id: Mapped[str] = mapped_column(String(64))
    business_id: Mapped[str] = mapped_column(String(64), default="")
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)
    # A quien le llega el correo agrupado.
    emails: Mapped[list[str]] = mapped_column(JSON, default=list)
    # Telefono del duenio/encargada para el aviso por WhatsApp. Solo se usa
    # si su ventana de 24h esta abierta (texto libre, gratis); si esta
    # cerrada, el aviso urgente cae a esta plantilla y el resto espera al
    # correo. Ver core/alerts.py.
    whatsapp_to: Mapped[str] = mapped_column(String(32), default="")
    urgent_template: Mapped[str] = mapped_column(String(191), default="")
    urgent_template_language: Mapped[str] = mapped_column(String(16), default="es")
    # Minutos que una conversacion puede quedarse sin responder antes de
    # que se avise. Por debajo de esto no se molesta a nadie: el bot suele
    # estar contestando.
    quiet_minutes: Mapped[int] = mapped_column(Integer, default=10)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)


class QuickReply(Base):
    """Respuestas guardadas para no escribir lo mismo veinte veces al dia.

    ("los precios", "como llegar", "horarios"). El atajo es lo que se
    teclea en la bandeja para insertarlas; el texto es lo que sale.

    Viven en Connect y no en cada app a proposito: la bandeja es una sola
    y construir esto dos veces es justo lo que vuelve imposible escalar.
    """

    __tablename__ = "quick_replies"
    __table_args__ = (
        UniqueConstraint("app_id", "business_id", "shortcut", name="uq_quick_reply_shortcut"),
    )

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=_uuid)
    app_id: Mapped[str] = mapped_column(String(64))
    business_id: Mapped[str] = mapped_column(String(64), default="")
    # Sin la barra: se escribe "/precios" y se guarda "precios".
    shortcut: Mapped[str] = mapped_column(String(64))
    title: Mapped[str] = mapped_column(String(128), default="")
    text: Mapped[str] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)
