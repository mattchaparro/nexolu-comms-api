"""Backfill: NEXOLU_APPS_JSON -> tablas comms_apps/provider_credentials.

Idempotente: cualquier `app_id` que ya exista en la BD se salta tal cual
(no se sobreescribe). Preserva el `api_key` EXISTENTE de cada app (no genera
uno nuevo) para que POS/Spa/EasyTickets no necesiten cambiar nada de su
lado - ver core/auth/apps.py para el fallback de transicion que consume
mientras este script no haya corrido en un ambiente.

Uso: `python scripts/migrate_apps_json_to_db.py` (lee `NEXOLU_APPS_JSON` y
`DATABASE_URL` del entorno/`.env`, igual que el servicio).
"""
from __future__ import annotations

import asyncio

from nexolu_comms_api.config import get_settings
from nexolu_comms_api.core.auth.repository import CommsAppRepository, ProviderCredentialRepository
from nexolu_comms_api.core.db.session import get_sessionmaker, init_models


def _whatsapp_fields(whatsapp) -> tuple[dict, dict]:
    config = {
        "phone_number_id": whatsapp.phone_number_id,
        "waba_id": whatsapp.waba_id,
        "callback_url": whatsapp.callback_url,
    }
    secrets = {
        "access_token": whatsapp.access_token,
        "webhook_verify_token": whatsapp.webhook_verify_token,
        "meta_app_secret": whatsapp.meta_app_secret,
        "callback_secret": whatsapp.callback_secret,
    }
    return config, secrets


def _email_fields(email) -> tuple[dict, dict]:
    config = {"from_email": email.from_email, "from_name": email.from_name}
    secrets = {"brevo_api_key": email.brevo_api_key}
    return config, secrets


async def migrate() -> None:
    settings = get_settings()
    await init_models()

    created_apps: list[str] = []
    skipped_apps: list[str] = []

    async with get_sessionmaker()() as session:
        app_repo = CommsAppRepository(session)
        credential_repo = ProviderCredentialRepository(session)

        for app_id, registration in settings.apps.items():
            existing = await app_repo.get_by_app_id(app_id)
            if existing is not None:
                skipped_apps.append(app_id)
                continue

            app = await app_repo.create(
                app_id=app_id, name=registration.name or app_id, api_key=registration.api_key
            )

            if registration.whatsapp is not None:
                config, secrets = _whatsapp_fields(registration.whatsapp)
                await credential_repo.upsert(
                    app_id=app.id, provider_slug="meta_whatsapp", config=config, secrets=secrets
                )

            if registration.email is not None:
                config, secrets = _email_fields(registration.email)
                await credential_repo.upsert(app_id=app.id, provider_slug="brevo", config=config, secrets=secrets)

            created_apps.append(app_id)

        await session.commit()

    print(f"Apps creadas: {created_apps or '(ninguna)'}")
    print(f"Apps ya existentes (saltadas): {skipped_apps or '(ninguna)'}")


if __name__ == "__main__":
    asyncio.run(migrate())
