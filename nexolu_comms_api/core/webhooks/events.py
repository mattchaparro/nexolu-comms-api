"""Clasificacion superficial de un evento de Meta, para el panel.

Esto NO viola el principio de "el servicio nunca interpreta el contenido":
el payload viaja intacto a la app duena. Aca solo se extraen dos metadatos
de la envoltura estandar de Graph API (que tipo de evento es, y de que
numero) para que el panel pueda filtrar "pedidos sin entregar" o "eventos
del numero X" sin abrir cada JSON a mano. Si el payload no tiene la forma
esperada, se clasifica como `unknown` y se reenvia igual.
"""
from __future__ import annotations

import json


def classify(payload: bytes) -> tuple[str, str | None]:
    """@return (event_type, phone_number_id). Nunca lanza."""
    try:
        change = json.loads(payload)["entry"][0]["changes"][0]
        field = change.get("field")
        value = change.get("value") or {}
    except (ValueError, KeyError, IndexError, TypeError):
        return "unknown", None

    phone_number_id = (value.get("metadata") or {}).get("phone_number_id")

    if field == "messages":
        if value.get("statuses"):
            return "status", phone_number_id
        messages = value.get("messages") or []
        message_type = messages[0].get("type") if messages and isinstance(messages[0], dict) else None
        # `order` (carrito enviado por el cliente) se distingue del resto de
        # mensajes porque es el evento que jamas debe perderse sin que se note.
        return ("order" if message_type == "order" else "message"), phone_number_id

    if field == "message_template_status_update":
        return "template", phone_number_id
    if field == "account_update":
        return "account", phone_number_id

    return field or "unknown", phone_number_id
