"""El registro de la bandeja (chat_messages), compartido entre quienes envian.

Tres origenes escriben salientes en el hilo: el motor de flujos (`flow`),
el panel (`panel`) y la API de las apps (`api` - p.ej. el bot del spa
respondiendo via /v1/notifications/send). Todos pasan por aca para que la
burbuja se pinte igual venga de donde venga.

Quien es "esta persona" lo decide UNA sola regla, la del motor
(`ContactRepository.get_or_create`): el webhook del numero compartido no
sabe el negocio y la app si, asi que buscar solo por la terna exacta
partia la conversacion en dos contactos.
"""
from __future__ import annotations

from datetime import datetime, timedelta
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from nexolu_comms_api.core.channels.base import ChannelSendResult, OutboundMessage
from nexolu_comms_api.core.db.entities import ChatMessage, Contact


def outbound_chat_type(message: OutboundMessage) -> str:
    if message.media_kind:
        return message.media_kind
    if message.template_name:
        return "template"
    if message.list_rows:
        return "list"
    if message.buttons:
        return "buttons"
    if message.cta_url:
        return "cta_url"
    if message.product_retailer_id or message.product_sections or message.send_catalog:
        return "product"
    return "text"


def outbound_chat_body(message: OutboundMessage) -> str:
    """Lo que se lee en la burbuja del hilo.

    Una plantilla no lleva texto propio -- el cuerpo real lo tiene Meta --,
    asi que se guarda lo que el negocio ENVIO: nombre y variables. Sin esto
    las plantillas salen como burbujas vacias y el hilo se vuelve ilegible
    justo donde mas importa (el mensaje con el que se reabre una
    conversacion fria).
    """
    if message.text:
        return message.text
    if message.media_caption:
        return message.media_caption
    if message.template_name:
        params = [
            str(p.get("text", ""))
            for component in message.template_components
            if str(component.get("type", "")).lower() == "body"
            for p in component.get("parameters", [])
        ]
        etiqueta = f"[plantilla {message.template_name}]"
        return f"{etiqueta} " + " · ".join(params) if params else etiqueta
    return ""


def outbound_chat_payload(message: OutboundMessage) -> dict[str, Any]:
    """Lo minimo para pintar la burbuja rica en la bandeja."""
    payload: dict[str, Any] = {}
    if message.buttons:
        payload["buttons"] = message.buttons
    if message.list_rows:
        payload["rows"] = message.list_rows
        payload["list_button"] = message.list_button
    if message.cta_url:
        payload["cta_url"] = message.cta_url
        payload["cta_title"] = message.cta_title
    if message.media_kind:
        payload["media_kind"] = message.media_kind
        payload["media_url"] = message.media_url
    if message.template_name:
        payload["template"] = message.template_name
        payload["language"] = message.template_language
        # Los valores tal cual, en su orden. El cuerpo los junta con " · "
        # para leerse, y eso no se puede deshacer si un valor trae un punto
        # medio; con estos se arma la plantilla exacta al mostrarla
        # (ver render_template_bubble).
        payload["template_params"] = _template_params(message.template_components, "body")
        payload["template_header_params"] = _template_params(message.template_components, "header")
    return payload


def _template_params(components: list[dict[str, Any]], kind: str) -> list[str]:
    return [
        str(p.get("text", ""))
        for component in components
        if str(component.get("type", "")).lower() == kind
        for p in component.get("parameters", [])
    ]


def render_template_bubble(
    components: list[dict[str, Any]],
    body_params: list[str],
    header_params: list[str] | None = None,
) -> tuple[str, list[dict[str, str]]]:
    """La plantilla como la RECIBIO la persona: texto y botones.

    La burbuja mostraba "[plantilla cita_nueva_equipo] Marcela · Carolina ·
    ..." porque se guardaba solo lo que el negocio envio. Con el texto
    aprobado en Meta (el espejo de `whatsapp_templates`) se rellenan los
    {{1}}, {{2}}... y quien mira la bandeja lee exactamente lo que llego.

    Un marcador sin valor se deja como estaba: mejor ver "{{3}}" que un
    hueco que parezca que el mensaje salio incompleto.
    """
    def rellenar(texto: str, valores: list[str]) -> str:
        for i, valor in enumerate(valores, start=1):
            texto = texto.replace("{{" + str(i) + "}}", valor)
        return texto

    partes: list[str] = []
    botones: list[dict[str, str]] = []

    for component in components:
        tipo = str(component.get("type", "")).upper()
        if tipo == "HEADER" and str(component.get("format", "TEXT")).upper() == "TEXT":
            partes.append("*" + rellenar(str(component.get("text", "")), header_params or []) + "*")
        elif tipo == "BODY":
            partes.append(rellenar(str(component.get("text", "")), body_params))
        elif tipo == "FOOTER":
            partes.append("_" + str(component.get("text", "")) + "_")
        elif tipo == "BUTTONS":
            for i, boton in enumerate(component.get("buttons", [])):
                titulo = str(boton.get("text", "")).strip()
                if titulo:
                    botones.append({"id": f"b{i}", "title": titulo})

    return "\n\n".join(p for p in partes if p.strip("*_ ")), botones


def log_outbound_chat(
    session: AsyncSession,
    *,
    contact: Contact,
    message: OutboundMessage,
    result: ChannelSendResult,
    origin: str,
) -> ChatMessage:
    payload = outbound_chat_payload(message)
    # Por qué no salió, en palabras de Meta. Sin esto la bandeja muestra
    # la respuesta del bot como si hubiera llegado: así pasó con una
    # clienta que escribió tres veces al número de prueba y Meta rechazó
    # las tres respuestas ("not in allowed list") sin que nadie se enterara.
    if result.status == "failed" and result.error:
        payload = {**payload, "error": result.error[:300]}

    row = ChatMessage(
        app_id=contact.app_id,
        business_id=contact.business_id,
        contact_id=contact.id,
        direction="out",
        message_type=outbound_chat_type(message),
        body=outbound_chat_body(message),
        payload=payload,
        wamid=result.provider_message_id,
        status=result.status,
        origin=origin,
    )
    session.add(row)
    return row


async def resolve_contact_for_send(
    session: AsyncSession, app_id: str, business_id: str, phone: str
) -> Contact:
    """El contacto al que se le esta escribiendo, sin partir el hilo.

    Misma regla que usa el motor al recibir (ContactRepository): una sola
    definicion de "quien es esta persona", porque tener dos fue exactamente
    lo que dejo la conversacion partida en dos contactos.
    """
    from nexolu_comms_api.core.flows.engine import ContactRepository

    contact = await ContactRepository(session).get_or_create(app_id, business_id, phone)
    await session.flush()
    return contact


# Orden en que avanza un mensaje. Un acuse que llega tarde --el "entregado"
# despues del "leido"-- no puede hacerlo retroceder.
_AVANCE = {"sent": 1, "delivered": 2, "read": 3}


async def apply_chat_statuses_from_event(event_id: str) -> None:
    """Refleja en la bandeja lo que Meta dice que paso con cada envio.

    Meta acepta un mensaje y lo rechaza segundos despues, por un aviso
    aparte. Sin esto la bandeja dejaba el check verde de "enviado" en
    mensajes que nunca llegaron: los dos avisos a Marcela se veian enviados
    mientras Meta los habia rechazado.

    Se empareja por el wamid. Los acuses de mensajes que no pasaron por
    Connect --otra app suscrita al mismo numero-- simplemente no se
    encuentran. Nunca lanza: es un efecto secundario del webhook.
    """
    import json
    import logging

    from sqlalchemy import select

    from nexolu_comms_api.core.db.entities import WebhookEvent
    from nexolu_comms_api.core.db.session import get_sessionmaker

    logger = logging.getLogger(__name__)

    async with get_sessionmaker()() as session:
        event = await session.get(WebhookEvent, event_id)
        if event is None:
            return

        try:
            data = json.loads(event.payload)
            estados = [
                s
                for entry in data.get("entry", [])
                for change in entry.get("changes", [])
                for s in (change.get("value", {}).get("statuses") or [])
            ]
        except (ValueError, TypeError, AttributeError):
            logger.warning("chats.status_unparseable", extra={"event_id": event_id})
            return

        for estado in estados:
            wamid = estado.get("id")
            nuevo = estado.get("status")
            if not wamid or not nuevo:
                continue

            fila = (
                await session.execute(select(ChatMessage).where(ChatMessage.wamid == wamid))
            ).scalars().first()
            if fila is None:
                continue

            if nuevo == "failed":
                error = (estado.get("errors") or [{}])[0]
                detalle = (error.get("error_data") or {}).get("details") or error.get("message") or error.get("title")
                if error.get("code") == 131047:
                    detalle = (
                        "La persona no le ha escrito a este numero en las ultimas 24 horas "
                        "y el mensaje salio como texto libre en vez de plantilla."
                    )
                fila.status = "failed"
                fila.payload = {**(fila.payload or {}), "error": f"No se entrego: {detalle}"[:300]}
            elif fila.status != "failed" and _AVANCE.get(nuevo, 0) > _AVANCE.get(fila.status or "", 0):
                fila.status = nuevo

        await session.commit()


WINDOW_HOURS = 24


class WindowChecker:
    """¿Se le puede escribir texto libre a este contacto AHORA?

    La ventana de 24 h de Meta es entre la persona y UN numero, no entre la
    persona y el negocio. Si el negocio cambia de numero (Luxury paso del
    304 al 301), quien le escribio al viejo tiene la ventana abierta con el
    viejo; un texto desde el nuevo no le llega. Por eso se compara el
    numero al que escribio (`last_inbound_phone_number_id`) con el que hoy
    envia para su app y negocio.

    Cachea el numero que envia por (app, negocio): la bandeja pinta decenas
    de filas y resolverlo en cada una seria una consulta por fila.
    """

    def __init__(self, session: AsyncSession) -> None:
        self._session = session
        self._senders: dict[tuple[str, str], str | None] = {}

    async def sender_number(self, app_id: str, business_id: str) -> str | None:
        key = (app_id, business_id)
        if key not in self._senders:
            from nexolu_comms_api.core.auth.apps import resolve_by_app_id
            from nexolu_comms_api.core.channels.business_channels import resolve_whatsapp_identity

            app = await resolve_by_app_id(self._session, app_id)
            number: str | None = None
            if app is not None and app.whatsapp is not None:
                identity, _ = await resolve_whatsapp_identity(self._session, app, business_id or app_id)
                number = identity.whatsapp.phone_number_id if identity.whatsapp else None
            self._senders[key] = number
        return self._senders[key]

    async def is_open(self, contact: Contact, now: datetime | None = None) -> bool:
        now = now or datetime.utcnow()
        if not contact.last_inbound_at or contact.last_inbound_at <= now - timedelta(hours=WINDOW_HOURS):
            return False
        # Filas de antes de guardar el numero: se confia en la fecha, como
        # siempre se hizo (al cambiar de numero se rellenan, ver runbook).
        if not contact.last_inbound_phone_number_id:
            return True
        sender = await self.sender_number(contact.app_id, contact.business_id)
        return sender is None or sender == contact.last_inbound_phone_number_id
