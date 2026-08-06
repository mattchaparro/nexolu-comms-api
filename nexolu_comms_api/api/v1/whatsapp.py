"""Micro-interacciones propias de WhatsApp que no encajan en el envio
generico de POST /v1/notifications/send (marcar leido + "escribiendo..." no
es "enviar una notificacion" - es responder a un mensaje que YA llego). Vive
aparte para no forzar el contrato multi-canal a modelar algo que solo un
canal soporta.
"""
from __future__ import annotations

from fastapi import APIRouter, Depends
from pydantic import BaseModel

from nexolu_comms_api.config import get_settings
from nexolu_comms_api.core.auth.apps import AppIdentity
from nexolu_comms_api.core.auth.dependencies import get_current_app
from nexolu_comms_api.core.channels.whatsapp import WhatsAppChannel

router = APIRouter(prefix="/v1/whatsapp", tags=["whatsapp"])


class ReadReceiptIn(BaseModel):
    to: str
    message_id: str


class ReadReceiptOut(BaseModel):
    status: str  # sent | skipped


@router.post("/read-receipt", response_model=ReadReceiptOut)
async def mark_as_read(payload: ReadReceiptIn, app: AppIdentity = Depends(get_current_app)) -> ReadReceiptOut:
    if app.whatsapp is None:
        return ReadReceiptOut(status="skipped")

    ok = await WhatsAppChannel(get_settings()).mark_as_read_with_typing(app, payload.to, payload.message_id)

    return ReadReceiptOut(status="sent" if ok else "skipped")
