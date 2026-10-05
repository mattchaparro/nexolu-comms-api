"""Los archivos que manda la clienta (audio, imagen, video, documento).

Meta no los entrega en el webhook: manda un `id` de media, y el archivo se
pide a la Graph API con el token del numero (dos pasos: el id da una URL
temporal, y la URL da los bytes, ambas con Bearer). El chat de Connect
mostraba solo la palabra «audio»: no habia quien hiciera esos dos pasos.

Se guardan en disco la primera vez que alguien los abre (`MEDIA_DIR/
inbound/<id del mensaje>`): Meta los borra a los 30 dias y la URL vence en
minutos, asi que volver a escuchar un audio no puede depender de ella.

Y lo contrario: `to_whatsapp_voice` convierte lo que graba el navegador
(webm/opus en Chrome, mp4/aac en Safari) a ogg/opus, que es lo que WhatsApp
acepta como nota de voz. Con ffmpeg: copiar el opus a un ogg no alcanza
para el aac de Safari, asi que se recodifica siempre (es un audio corto).
"""
from __future__ import annotations

import asyncio
import mimetypes
import shutil
import tempfile
from pathlib import Path

import httpx

from nexolu_comms_api.core.auth.apps import AppIdentity

MEDIA_TYPES = ("audio", "image", "video", "document", "sticker")


class MediaUnavailable(Exception):
    """Meta no lo entrego (vencio, token sin permiso, Meta caida)."""


async def download_from_meta(
    app: AppIdentity, media_id: str, base_url: str, timeout: float
) -> tuple[bytes, str]:
    """@return (bytes, mime). Lanza MediaUnavailable si no se pudo."""
    if app.whatsapp is None or not media_id:
        raise MediaUnavailable("Sin numero de WhatsApp configurado.")

    headers = {"Authorization": f"Bearer {app.whatsapp.access_token}"}
    try:
        async with httpx.AsyncClient(timeout=timeout) as client:
            meta = await client.get(f"{base_url.rstrip('/')}/{media_id}", headers=headers)
            if meta.is_error:
                raise MediaUnavailable(f"Meta respondio {meta.status_code} al pedir el archivo.")
            info = meta.json()
            archivo = await client.get(str(info.get("url") or ""), headers=headers)
            if archivo.is_error:
                raise MediaUnavailable(f"Meta respondio {archivo.status_code} al descargarlo.")
    except httpx.HTTPError as exc:
        raise MediaUnavailable(f"No se pudo contactar a Meta: {exc}") from exc

    mime = str(info.get("mime_type") or archivo.headers.get("content-type") or "application/octet-stream")
    return archivo.content, mime.split(";")[0].strip()


def extension_for(mime: str) -> str:
    """`audio/ogg` -> `.ogg`. Lo que no se reconoce queda sin extension."""
    especiales = {"audio/ogg": ".ogg", "audio/mpeg": ".mp3", "audio/mp4": ".m4a", "image/jpeg": ".jpg"}
    return especiales.get(mime) or mimetypes.guess_extension(mime) or ""


async def to_whatsapp_voice(content: bytes, suffix: str) -> bytes:
    """Lo grabado en el navegador, como ogg/opus mono. Lanza RuntimeError si falla."""
    ffmpeg = shutil.which("ffmpeg")
    if ffmpeg is None:
        raise RuntimeError("ffmpeg no esta instalado: no se pueden convertir notas de voz.")

    with tempfile.TemporaryDirectory() as carpeta:
        entrada = Path(carpeta) / f"entrada{suffix or '.webm'}"
        salida = Path(carpeta) / "salida.ogg"
        entrada.write_bytes(content)
        proceso = await asyncio.create_subprocess_exec(
            ffmpeg, "-y", "-loglevel", "error", "-i", str(entrada),
            "-vn", "-ac", "1", "-c:a", "libopus", "-b:a", "32k", str(salida),
            stdout=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.PIPE,
        )
        _, error = await proceso.communicate()
        if proceso.returncode != 0 or not salida.exists():
            raise RuntimeError(f"No se pudo convertir el audio: {error.decode(errors='ignore')[:300]}")
        return salida.read_bytes()
