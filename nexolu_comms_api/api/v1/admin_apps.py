"""CRUD administrativo de apps cliente (`CommsApp`).

Protegido con `require_platform_access` (NEXOLU_PLATFORM_API_KEY): el mismo
nivel de acceso que ya usa GET /v1/platform/usage y /v1/platform/notifications
para ver datos de TODAS las apps. Ninguna app integradora conoce esta key.

La api_key en texto plano solo se devuelve en la respuesta de creacion y de
regeneracion - despues de eso, el servicio la trata como un secreto que no
vuelve a mostrar (aunque la guarda cifrada, ver core/security/crypto.py).
"""
from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy.ext.asyncio import AsyncSession

from nexolu_comms_api.core.auth.dependencies import (
    get_panel_scope,
    require_platform_access,
)
from nexolu_comms_api.core.auth.panel import PanelScope
from nexolu_comms_api.core.auth.repository import CommsAppRepository, ProviderCredentialRepository
from nexolu_comms_api.core.db.entities import CommsApp
from nexolu_comms_api.core.db.session import get_session
from nexolu_comms_api.core.schemas import CommsAppCreatedOut, CommsAppIn, CommsAppOut, CommsAppPatch

# El LISTADO es por scope (un cliente externo ve sus propias apps - es su
# pantalla de inicio en Connect); crear apps, editarlas y rotar api_keys
# sigue siendo SOLO plataforma, por eso esas rutas declaran
# `require_platform_access` una a una en vez de heredarlo del router.
router = APIRouter(prefix="/v1/admin/apps", tags=["admin"])


def _mask(api_key: str) -> str:
    if len(api_key) <= 8:
        return "*" * len(api_key)
    return f"{api_key[:6]}...{api_key[-4:]}"


async def _to_out(session: AsyncSession, app: CommsApp) -> CommsAppOut:
    credentials = await ProviderCredentialRepository(session).list_for_app(app.id)
    slugs = {c.provider_slug for c in credentials}
    return CommsAppOut(
        id=app.id,
        app_id=app.app_id,
        name=app.name,
        api_key_masked=_mask(app.api_key),
        is_active=app.is_active,
        has_meta_whatsapp="meta_whatsapp" in slugs,
        has_brevo="brevo" in slugs,
        created_at=app.created_at,
        updated_at=app.updated_at,
    )


async def _to_created_out(session: AsyncSession, app: CommsApp) -> CommsAppCreatedOut:
    out = await _to_out(session, app)
    return CommsAppCreatedOut(**out.model_dump(), api_key=app.api_key)


async def _get_or_404(repo: CommsAppRepository, app_id: str) -> CommsApp:
    app = await repo.get_by_app_id(app_id)
    if app is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=f"App '{app_id}' no existe.")
    return app


@router.get("", response_model=list[CommsAppOut])
async def list_apps(
    scope: PanelScope = Depends(get_panel_scope),
    session: AsyncSession = Depends(get_session),
) -> list[CommsAppOut]:
    apps = await CommsAppRepository(session).list_all()
    apps = [app for app in apps if scope.allows(app.app_id)]
    return [await _to_out(session, app) for app in apps]


@router.post(
    "", response_model=CommsAppCreatedOut, status_code=status.HTTP_201_CREATED,
    dependencies=[Depends(require_platform_access)],
)
async def create_app(payload: CommsAppIn, session: AsyncSession = Depends(get_session)) -> CommsAppCreatedOut:
    repo = CommsAppRepository(session)

    if await repo.get_by_app_id(payload.app_id) is not None:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT, detail=f"La app '{payload.app_id}' ya esta registrada."
        )

    app = await repo.create(**payload.model_dump())
    await session.commit()
    return await _to_created_out(session, app)


@router.patch("/{app_id}", response_model=CommsAppOut, dependencies=[Depends(require_platform_access)])
async def update_app(
    app_id: str, payload: CommsAppPatch, session: AsyncSession = Depends(get_session)
) -> CommsAppOut:
    repo = CommsAppRepository(session)
    app = await _get_or_404(repo, app_id)

    app = await repo.update(app, **payload.model_dump(exclude_unset=True))
    await session.commit()
    return await _to_out(session, app)


@router.post(
    "/{app_id}/regenerate-key", response_model=CommsAppCreatedOut,
    dependencies=[Depends(require_platform_access)],
)
async def regenerate_key(app_id: str, session: AsyncSession = Depends(get_session)) -> CommsAppCreatedOut:
    repo = CommsAppRepository(session)
    app = await _get_or_404(repo, app_id)

    app = await repo.regenerate_key(app)
    await session.commit()
    return await _to_created_out(session, app)
