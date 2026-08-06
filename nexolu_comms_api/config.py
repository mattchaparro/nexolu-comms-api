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
    conocen y para que el gasto/limites de Meta queden segregados por app."""

    phone_number_id: str
    access_token: str
    waba_id: str | None = None


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

    @property
    def apps(self) -> dict[str, AppRegistration]:
        raw = json.loads(self.nexolu_apps_json or "{}")
        return {app_id: AppRegistration(**data) for app_id, data in raw.items()}


@lru_cache
def get_settings() -> Settings:
    return Settings()
