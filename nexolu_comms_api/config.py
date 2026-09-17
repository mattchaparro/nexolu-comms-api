"""Configuracion central del servicio.

Todo lo que varia entre entornos (desarrollo, staging, produccion) vive aqui,
leido de variables de entorno. Nada de esto es logica de negocio de ningun
producto: son credenciales de proveedores de mensajeria y el registro de que
aplicaciones (POS, Spa, EasyTickets...) pueden llamar al servicio.
"""
from __future__ import annotations

import json
from functools import lru_cache

from pydantic import BaseModel
from pydantic_settings import BaseSettings, SettingsConfigDict


class WhatsAppAppConfig(BaseModel):
    """Credenciales de WhatsApp Cloud API (Meta) de UNA app. Cada app tiene su
    propio numero/WABA - no se comparte uno solo entre todo el ecosistema,
    para que el negocio le hable a sus clientes desde el numero que ya
    conocen y para que el gasto/limites de Meta queden segregados por app.

    Meta registra el webhook a nivel de App/WABA, no por numero de telefono
    - por eso el webhook tambien es por app, no por negocio dentro de una
    app (ver GET/POST /webhooks/whatsapp/{app_id}). Tres secretos distintos,
    con dueños distintos:

    - `webhook_verify_token`: lo elige quien configura el webhook en el
      dashboard de Meta: Meta lo devuelve en el handshake GET para probar
      que quien pide suscribirse es el mismo que lo registro.
    - `meta_app_secret`: App Secret del dashboard de Meta (NO el access
      token). Verifica `X-Hub-Signature-256` en cada POST - prueba que el
      evento de verdad vino de Meta, no de un tercero que le pego a esta
      URL. Opcional: sin el, se salta esa verificacion (con warning en el
      log), no se bloquea el webhook completo por un dato que no todas las
      apps van a tener configurado desde el primer dia.
    - `callback_secret`: propio de este servicio (nunca lo ve Meta). Firma
      cada evento que se reenvia a `callback_url`, mismo patron HMAC que ya
      usa Nexolu Payments Core con sus apps cliente.
    """

    phone_number_id: str
    access_token: str
    waba_id: str | None = None
    webhook_verify_token: str | None = None
    meta_app_secret: str | None = None
    # Con esto en True, un webhook sin firma verificable (sin
    # `meta_app_secret` configurado, o con firma invalida) se rechaza con
    # 401 en vez de pasar con warning. Default False para no romper apps
    # existentes que aun no configuran el secret; se enciende POR APP desde
    # el panel cuando la app tiene trafico real - un evento falsificado que
    # se reenvia a una app de negocio es peor que un warning en el log.
    enforce_meta_signature: bool = False
    # Catalogo conectado a la WABA (uno solo por WABA, regla de Meta). Para
    # el numero compartido de la app vive en la credencial; para un numero
    # propio, en BusinessChannel.catalog_id - ver whatsapp_identity_for().
    catalog_id: str | None = None
    # Meta Business (portafolio) duenio de la WABA: hace falta para CREAR
    # un catalogo por API (POST /{business_id}/owned_product_catalogs).
    meta_business_id: str | None = None
    callback_secret: str | None = None
    # A donde se reenvia (firmado) cada evento entrante de esta app - este
    # servicio NUNCA interpreta el mensaje (texto, respuesta de Flow, etc.),
    # solo verifica la firma de Meta y lo reenvia intacto. Ver
    # core/webhooks/whatsapp.py.
    callback_url: str | None = None


class InstagramAppConfig(BaseModel):
    """Credenciales de publicacion en Instagram de UNA app.

    Separadas de las de WhatsApp aunque el negocio sea el mismo: son flujos
    de login y permisos distintos (`instagram_business_content_publish` vs.
    `whatsapp_business_messaging`), y el token de WhatsApp NO sirve aca.

    Ese token ademas CADUCA -- Meta emite tokens de larga duracion de 60
    dias, renovables. No hay equivalente al token permanente de usuario del
    sistema que se usa para WhatsApp, asi que quien opere esto tiene que
    acordarse de renovarlo o publicar dejara de funcionar sin aviso.
    """

    ig_user_id: str
    access_token: str
    username: str | None = None


class EmailAppConfig(BaseModel):
    """Identidad de remitente de UNA app. `brevo_api_key` es opcional: sin
    ella, el envio usa la API key de Brevo de PLATAFORMA (ver
    Settings.brevo_api_key) - la mayoria de apps no necesitan su propia
    cuenta de Brevo, solo su propio remitente dentro de la cuenta compartida."""

    from_email: str
    from_name: str = ""
    brevo_api_key: str | None = None


class AppRegistration(BaseSettings):
    """Una aplicacion cliente del servicio (POS, Spa, EasyTickets...).

    `whatsapp`/`email` son opcionales de forma independiente: una app puede
    tener solo uno de los dos canales configurado. Pedir un canal sin
    configurar para esa app no falla la llamada completa - ver
    ChannelSender.send(), que devuelve status="skipped" para ese canal
    puntual dentro de la respuesta multi-canal.
    """

    api_key: str
    name: str = ""
    whatsapp: WhatsAppAppConfig | None = None
    email: EmailAppConfig | None = None


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    # Persistencia. SQLite en desarrollo/tests (cero infraestructura); MySQL
    # en produccion: mysql+aiomysql://user:pass@host:3306/nexolu_comms.
    database_url: str = "sqlite+aiosqlite:///./nexolu_comms_api.db"

    # WhatsApp Cloud API: la version del Graph API es global (misma para
    # todas las apps), las credenciales (phone_number_id/access_token) son
    # por app - ver AppRegistration.whatsapp.
    whatsapp_api_base_url: str = "https://graph.facebook.com/v21.0"

    # Tarifa por categoria de plantilla, en micro-dolares (1_000_000 = US$1).
    # Meta cobra distinto segun la categoria de la plantilla que se envia -
    # mismos 4 valores que legacy/POS usaban de forma local antes de que
    # este servicio existiera. `service` es 0: son respuestas dentro de la
    # ventana de 24h, que Meta no cobra. Configurable porque Meta cambia
    # estas tarifas por pais/tiempo sin previo aviso.
    whatsapp_rate_marketing_micros: int = 25_000
    whatsapp_rate_utility_micros: int = 8_000
    whatsapp_rate_authentication_micros: int = 10_000
    whatsapp_rate_service_micros: int = 0

    # Brevo (email transaccional): API key de PLATAFORMA, usada por
    # cualquier app que no traiga la suya propia en AppRegistration.email.
    brevo_api_key: str = ""
    brevo_api_base_url: str = "https://api.brevo.com/v3"

    http_timeout_seconds: int = 20

    # Panel dedicado (nexolu-comms-front): un solo operador, credenciales en
    # env vars y JWT verificado 100% local - mismo patron deliberado que
    # nexolu-admin (el panel no puede depender de otro servicio para poder
    # entrar). Vacios por defecto = login siempre falla (fallar cerrado).
    # El JWT del panel vale como credencial de plataforma en /v1/admin/* y
    # /v1/platform/* (ver require_platform_access).
    panel_email: str = ""
    panel_full_name: str = "Operador Nexolu"
    panel_password_hash: str = ""  # bcrypt
    panel_jwt_secret: str = ""
    panel_jwt_ttl_hours: int = 24
    # Origenes permitidos para CORS del panel (coma-separados). Vacio = sin
    # CORS - las apps server-side no lo necesitan, solo el navegador del panel.
    panel_cors_origins: str = ""

    # SSO con nexolu-auth (auth.nexolu.co) - ver core/auth/sso.py. La llave
    # publica va fijada aca ({kid: PEM en base64}), nunca se hace fetch:
    # vacia = el canje responde 503 y el login local sigue intacto.
    nexolu_auth_issuer: str = "https://auth.nexolu.co"
    nexolu_auth_audience: str = "nexolu-connect"
    nexolu_auth_public_keys: str = "{}"

    # La App Meta de PLATAFORMA de Nexolu (una sola para todo el ecosistema):
    # con ella corre Embedded Signup (los negocios conectan su propia WABA a
    # traves de esta app) y a ella llegan los webhooks de esos numeros
    # propios (POST /webhooks/whatsapp/platform). Distinta de las
    # credenciales por app de `provider_credentials`, que son el numero
    # compartido historico de cada app. Vacias por defecto: sin ellas, el
    # onboarding responde 503 y el webhook de plataforma no acepta eventos.
    meta_platform_app_id: str = ""
    meta_platform_app_secret: str = ""
    meta_platform_webhook_verify_token: str = ""
    # Id de la configuracion de Facebook Login for Business que el front de
    # cada app necesita para abrir el popup de Embedded Signup. No es un
    # secreto, pero vive aca para que las apps lo consulten en vez de
    # copiarlo en N .env.
    meta_login_config_id: str = ""

    # Worker de reintento de webhooks (ver core/webhooks/forwarder.py):
    # corre dentro del mismo proceso uvicorn como task asyncio - no hay
    # cola externa a este volumen, y agregar Redis/Celery por esto seria
    # infraestructura sin retorno hoy. El flag existe para apagarlo en
    # tests o si algun dia el reintento se muda a un proceso aparte.
    webhook_retry_worker_enabled: bool = True
    webhook_retry_interval_seconds: int = 30

    # Reanudacion de nodos `delay` del motor de flujos (mismo patron y
    # mismas razones que el worker de arriba).
    flow_resume_worker_enabled: bool = True
    flow_resume_interval_seconds: int = 30

    # Multimedia subida desde el panel (bloque Imagen del builder): se
    # guarda plana en MEDIA_DIR y se sirve publica en /media/<nombre> -
    # Meta descarga por URL. MEDIA_BASE_URL en prod = https://comms.nexolu.co
    # (vacio = se arma con la URL de la request).
    media_dir: str = "./media"
    media_base_url: str = ""

    # Registro de apps cliente, como JSON crudo (parseado en `apps`).
    nexolu_apps_json: str = "{}"

    # Credencial de PLATAFORMA (Nexolu, no una app individual): da acceso a
    # GET /v1/platform/usage, que agrega el gasto por app_id de TODAS las
    # apps. Nunca se le entrega a una app integradora - esa usa su propia
    # api_key para ver solo su propio gasto en GET /v1/usage/*. Vacia por
    # defecto: sin ella, /v1/platform/usage responde 503 en vez de quedar
    # accesible sin proteccion. Mismo patron que Nexolu IA Core.
    nexolu_platform_api_key: str = ""

    log_level: str = "INFO"

    # Cifra en reposo las credenciales de `CommsApp`/`ProviderCredential`
    # (BD) - ver core/security/crypto.py. Vacia por defecto = ese modulo
    # falla cerrado (RuntimeError) en el primer intento de leer/escribir una
    # credencial, nunca un fallback a texto plano.
    comms_master_key: str = ""

    @property
    def apps(self) -> dict[str, AppRegistration]:
        raw = json.loads(self.nexolu_apps_json or "{}")
        return {app_id: AppRegistration(**data) for app_id, data in raw.items()}


@lru_cache
def get_settings() -> Settings:
    return Settings()
