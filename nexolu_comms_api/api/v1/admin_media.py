"""Carga de archivos multimedia para los flujos del panel.

El caso: el bloque "Imagen" del builder necesita una URL PUBLICA (Meta
descarga el archivo por su cuenta, nunca se le suben bytes). En vez de
obligar a Alejandro/los negocios a hostear las imagenes por fuera, el
panel las sube aca y comms-api las sirve en /media/<nombre> - esa URL es
la que viaja en el payload del mensaje.

Guardado plano en disco (`MEDIA_DIR`): un panel de spas no es un CDN, y
un volumen montado en el compose sobrevive los redeploys. El nombre es
un uuid + extension whitelisted: nada de paths del cliente.
"""
from __future__ import annotations

import uuid
from pathlib import Path

from fastapi import APIRouter, Depends, HTTPException, Request, UploadFile, status
from pydantic import BaseModel

from nexolu_comms_api.config import get_settings
from nexolu_comms_api.core.auth.dependencies import get_chat_scope
from nexolu_comms_api.core.auth.panel import PanelScope

router = APIRouter(prefix="/v1/admin/media", tags=["admin-media"])

# Lo que Meta acepta como media por link (imagen/video/audio/documento).
ALLOWED_EXTENSIONS = {
    ".jpg", ".jpeg", ".png", ".webp",
    ".mp4", ".3gp",
    ".mp3", ".ogg", ".aac", ".amr",
    ".pdf",
}

MAX_UPLOAD_BYTES = 10 * 1024 * 1024  # el limite de imagen de Meta es 5MB; video 16MB


class MediaOut(BaseModel):
    url: str
    filename: str


@router.post("", response_model=MediaOut, status_code=status.HTTP_201_CREATED)
async def upload_media(
    request: Request,
    file: UploadFile,
    _scope: PanelScope = Depends(get_chat_scope),
) -> MediaOut:
    extension = Path(file.filename or "").suffix.lower()
    if extension not in ALLOWED_EXTENSIONS:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=f"Extension no permitida: {extension or '(sin extension)'}. "
            f"Validas: {', '.join(sorted(ALLOWED_EXTENSIONS))}",
        )

    content = await file.read()
    if len(content) > MAX_UPLOAD_BYTES:
        raise HTTPException(
            status_code=status.HTTP_413_REQUEST_ENTITY_TOO_LARGE,
            detail=f"El archivo pesa {len(content) // (1024 * 1024)}MB; maximo 10MB.",
        )

    settings = get_settings()
    media_dir = Path(settings.media_dir)
    media_dir.mkdir(parents=True, exist_ok=True)

    name = f"{uuid.uuid4().hex}{extension}"
    (media_dir / name).write_bytes(content)

    base = settings.media_base_url.rstrip("/") or str(request.base_url).rstrip("/")
    return MediaOut(url=f"{base}/media/{name}", filename=name)
