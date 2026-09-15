"""Configuracion administrativa de credenciales de proveedor (Meta WhatsApp
Cloud API, Brevo) por app.

Autorizado por SCOPE (`get_panel_scope`): la plataforma configura las
credenciales de cualquier app; un cliente externo del panel Connect, SOLO
las de sus propias apps (son SU WABA y SU cuenta de Brevo - configurarlas
es parte de usar Connect como producto). Dos
endpoints tipados por proveedor (no uno generico por `provider_slug` con un
dict crudo) para que FastAPI valide cada forma distinta - Meta necesita
phone_number_id/access_token/... y Brevo necesita from_email/brevo_api_key,
formas que no comparten campos.

"Rotar" una credencial = volver a llamar el mismo POST con el valor nuevo
(ver ProviderCredentialRepository.upsert): ni Meta ni Brevo exponen una API
para generar el secreto nuevo, siempre es "el operador ya lo genero en el
dashboard del proveedor y lo pega aca" - no hace falta un endpoint de
"rotate" separado de "configure".
"""
from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy.ext.asyncio import AsyncSession

from nexolu_comms_api.core.auth.dependencies import get_panel_scope, require_scope_for_app
from nexolu_comms_api.core.auth.panel import PanelScope
from nexolu_comms_api.core.auth.repository import CommsAppRepository, ProviderCredentialRepository
from nexolu_comms_api.core.db.session import get_session
from nexolu_comms_api.core.schemas import (
    BrevoIn,
    BrevoSecretsOut,
    BrevoStatusOut,
    MetaInstagramIn,
    MetaInstagramSecretsOut,
    MetaInstagramStatusOut,
    MetaWhatsAppIn,
    MetaWhatsAppSecretsOut,
    MetaWhatsAppStatusOut,
)


async def _require_app_scope(app_id: str, scope: PanelScope = Depends(get_panel_scope)) -> None:
    # 404 para lo ajeno (no se confirma existencia), igual que el resto del
    # scoping del panel.
    require_scope_for_app(scope, app_id)


router = APIRouter(
    prefix="/v1/admin/apps/{app_id}/providers",
    tags=["admin"],
    dependencies=[Depends(_require_app_scope)],
)


async def _get_app_or_404(session: AsyncSession, app_id: str):
    app = await CommsAppRepository(session).get_by_app_id(app_id)
    if app is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=f"App '{app_id}' no existe.")
    return app


# -- Meta WhatsApp Cloud API -------------------------------------------------


@router.post("/meta-whatsapp", response_model=MetaWhatsAppStatusOut, status_code=status.HTTP_201_CREATED)
async def configure_meta_whatsapp(
    app_id: str, payload: MetaWhatsAppIn, session: AsyncSession = Depends(get_session)
) -> MetaWhatsAppStatusOut:
    app = await _get_app_or_404(session, app_id)

    config = {
        "phone_number_id": payload.phone_number_id,
        "waba_id": payload.waba_id,
        "callback_url": payload.callback_url,
        "enforce_meta_signature": payload.enforce_meta_signature,
        "catalog_id": payload.catalog_id,
        "meta_business_id": payload.meta_business_id,
    }
    secrets = {
        "access_token": payload.access_token,
        "webhook_verify_token": payload.webhook_verify_token,
        "meta_app_secret": payload.meta_app_secret,
        "callback_secret": payload.callback_secret,
    }
    credential = await ProviderCredentialRepository(session).upsert(
        app_id=app.id, provider_slug="meta_whatsapp", config=config, secrets=secrets
    )
    await session.commit()

    return MetaWhatsAppStatusOut(configured=True, **credential.config)


@router.get("/meta-whatsapp", response_model=MetaWhatsAppStatusOut)
async def get_meta_whatsapp_status(app_id: str, session: AsyncSession = Depends(get_session)) -> MetaWhatsAppStatusOut:
    app = await _get_app_or_404(session, app_id)
    credential = await ProviderCredentialRepository(session).get_active(app.id, "meta_whatsapp")

    if credential is None:
        return MetaWhatsAppStatusOut(configured=False)
    return MetaWhatsAppStatusOut(configured=True, **credential.config)


@router.get("/meta-whatsapp/secrets", response_model=MetaWhatsAppSecretsOut)
async def get_meta_whatsapp_secrets(
    app_id: str, session: AsyncSession = Depends(get_session)
) -> MetaWhatsAppSecretsOut:
    app = await _get_app_or_404(session, app_id)
    credential = await ProviderCredentialRepository(session).get_active(app.id, "meta_whatsapp")

    if credential is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="Meta WhatsApp no esta configurado para esta app."
        )
    return MetaWhatsAppSecretsOut(**credential.secrets)


# -- Meta Instagram (publicacion) ---------------------------------------------
#
# Separado de meta-whatsapp aunque el negocio sea el mismo: son credenciales
# distintas, con permisos distintos, y una caduca (Instagram, 60 dias) y la
# otra no. Meterlas juntas haria que rotar una obligara a repegar la otra.


@router.post("/meta-instagram", response_model=MetaInstagramStatusOut, status_code=status.HTTP_201_CREATED)
async def configure_meta_instagram(
    app_id: str, payload: MetaInstagramIn, session: AsyncSession = Depends(get_session)
) -> MetaInstagramStatusOut:
    app = await _get_app_or_404(session, app_id)

    credential = await ProviderCredentialRepository(session).upsert(
        app_id=app.id,
        provider_slug="meta_instagram",
        config={"ig_user_id": payload.ig_user_id, "username": payload.username},
        secrets={"access_token": payload.access_token},
    )
    await session.commit()

    return MetaInstagramStatusOut(configured=True, **credential.config)


@router.get("/meta-instagram", response_model=MetaInstagramStatusOut)
async def get_meta_instagram_status(
    app_id: str, session: AsyncSession = Depends(get_session)
) -> MetaInstagramStatusOut:
    app = await _get_app_or_404(session, app_id)
    credential = await ProviderCredentialRepository(session).get_active(app.id, "meta_instagram")

    if credential is None:
        return MetaInstagramStatusOut(configured=False)
    return MetaInstagramStatusOut(configured=True, **credential.config)


@router.get("/meta-instagram/secrets", response_model=MetaInstagramSecretsOut)
async def get_meta_instagram_secrets(
    app_id: str, session: AsyncSession = Depends(get_session)
) -> MetaInstagramSecretsOut:
    app = await _get_app_or_404(session, app_id)
    credential = await ProviderCredentialRepository(session).get_active(app.id, "meta_instagram")

    if credential is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="Instagram no esta configurado para esta app."
        )
    return MetaInstagramSecretsOut(**credential.secrets)


# -- Brevo --------------------------------------------------------------------


@router.post("/brevo", response_model=BrevoStatusOut, status_code=status.HTTP_201_CREATED)
async def configure_brevo(app_id: str, payload: BrevoIn, session: AsyncSession = Depends(get_session)) -> BrevoStatusOut:
    app = await _get_app_or_404(session, app_id)

    config = {"from_email": payload.from_email, "from_name": payload.from_name}
    secrets = {"brevo_api_key": payload.brevo_api_key}
    credential = await ProviderCredentialRepository(session).upsert(
        app_id=app.id, provider_slug="brevo", config=config, secrets=secrets
    )
    await session.commit()

    return BrevoStatusOut(configured=True, **credential.config)


@router.get("/brevo", response_model=BrevoStatusOut)
async def get_brevo_status(app_id: str, session: AsyncSession = Depends(get_session)) -> BrevoStatusOut:
    app = await _get_app_or_404(session, app_id)
    credential = await ProviderCredentialRepository(session).get_active(app.id, "brevo")

    if credential is None:
        return BrevoStatusOut(configured=False)
    return BrevoStatusOut(configured=True, **credential.config)


@router.get("/brevo/secrets", response_model=BrevoSecretsOut)
async def get_brevo_secrets(app_id: str, session: AsyncSession = Depends(get_session)) -> BrevoSecretsOut:
    app = await _get_app_or_404(session, app_id)
    credential = await ProviderCredentialRepository(session).get_active(app.id, "brevo")

    if credential is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Brevo no esta configurado para esta app.")
    return BrevoSecretsOut(**credential.secrets)
