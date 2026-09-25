"""La bandeja / live chat de Connect (la de ManyChat).

Criticidad operativa: el numero del negocio en Cloud API no tiene app
movil ni SIM - esta bandeja web es la UNICA forma humana de leer la
conversacion y responder. El historial lo alimentan el webhook (todo
entrante), el motor de flujos y este mismo panel al responder.

La ventana de 24h manda: `window_open` avisa al front si un texto libre
entregaria; fuera de ventana el envio igual se intenta y Meta decide
(la fila queda con status failed, visible en el hilo).
"""
from __future__ import annotations

import unicodedata
from datetime import datetime
from typing import Any

from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException, status
from pydantic import BaseModel, Field, model_validator
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from nexolu_comms_api.core.auth.apps import resolve_by_app_id
from nexolu_comms_api.core.auth.dependencies import get_chat_scope
from nexolu_comms_api.core.auth.panel import PanelScope
from nexolu_comms_api.core.channels.base import OutboundMessage
from nexolu_comms_api.core.channels.business_channels import resolve_whatsapp_identity
from nexolu_comms_api.core.channels.registry import get_channel_registry
from nexolu_comms_api.core.chats import WindowChecker, log_outbound_chat, render_template_bubble
from nexolu_comms_api.core.db.entities import ChatMessage, Contact, PanelUser, WhatsAppTemplate
from nexolu_comms_api.core.db.session import get_session
from nexolu_comms_api.core.templates.service import TemplateRepository
from nexolu_comms_api.core.webhooks.app_events import post_app_event, post_contact_renamed

router = APIRouter(prefix="/v1/admin/chats", tags=["admin-chats"])

WINDOW_HOURS = 24

# Tope de hilos que se recorren para armar la bandeja. La busqueda y el
# contador de no leidos trabajan sobre esta ventana: es la conversacion
# reciente, que es lo que una bandeja atiende. El historial completo de un
# contacto se lee en su hilo, no en la lista.
MAX_CONVERSATIONS_SCANNED = 500


def _fold(text: str) -> str:
    """Minusculas y sin tildes, para comparar como escribe la gente: quien
    busca "acrilicas" tiene que encontrar "acrílicas" (y al reves)."""
    sin_tildes = unicodedata.normalize("NFD", text.strip().lower())
    return "".join(c for c in sin_tildes if unicodedata.category(c) != "Mn")


class ConversationOut(BaseModel):
    contact_id: str
    app_id: str
    business_id: str
    phone: str
    name: str
    last_body: str
    last_direction: str
    last_at: datetime
    window_open: bool
    unread: bool
    # La ultima respuesta no llego: Meta la rechazo. Para quien atiende es
    # lo mismo que no haber contestado, y se pinta asi.
    last_failed: bool = False
    assigned_to: str | None = None
    assigned_name: str | None = None


class ConversationListOut(BaseModel):
    items: list[ConversationOut]
    # Cuantas quedan sin leer EN TODO el scope, no solo en esta pagina: es
    # el numero que dice "hay gente esperando".
    unread_total: int
    has_more: bool


class ChatMessageOut(BaseModel):
    id: str
    direction: str
    message_type: str
    body: str
    payload: dict[str, Any]
    status: str
    origin: str
    created_at: datetime


class ChatTemplateIn(BaseModel):
    """La plantilla aprobada con la que se REABRE una conversacion fria.
    `params` es un texto por cada {{1}}, {{2}}... del cuerpo."""

    name: str = Field(min_length=1, max_length=191)
    language: str = Field(default="es", min_length=2, max_length=16)
    params: list[str] = Field(default_factory=list, max_length=20)


class ChatMediaIn(BaseModel):
    """Multimedia por link publico (Meta lo descarga; nunca se le suben
    bytes desde aca). El link sale de POST /v1/admin/media o de cualquier
    URL publica."""

    kind: str = Field(pattern="^(image|video|audio|document)$")
    url: str = Field(min_length=1, max_length=2048)
    caption: str | None = Field(default=None, max_length=1024)
    filename: str | None = Field(default=None, max_length=191)


class ChatSendIn(BaseModel):
    """Exactamente una forma: texto libre (solo entrega dentro de la
    ventana de 24h), plantilla (lo unico que entrega fuera) o multimedia."""

    text: str | None = Field(default=None, min_length=1, max_length=4096)
    template: ChatTemplateIn | None = None
    media: ChatMediaIn | None = None

    @model_validator(mode="after")
    def _exactly_one(self) -> ChatSendIn:
        given = [f for f in (self.text, self.template, self.media) if f is not None]
        if len(given) != 1:
            raise ValueError("Manda exactamente uno de 'text', 'template' o 'media'.")
        return self


class AssignIn(BaseModel):
    """`user_id` None = soltar la conversacion (vuelve a la bolsa comun)."""

    user_id: str | None = None


class ContactCardOut(BaseModel):
    """Lo que hay que saber de esta persona ANTES de contestarle.

    Solo lo que Connect SABE: telefono, como se llama, sus tags, sus
    campos, desde cuando escribe y las notas del equipo. Nada de citas ni
    de pedidos -- eso es de la app duena, que es quien lo sabe de verdad
    (principio 45); su ficha de negocio la pinta su propio panel al lado.
    """

    contact_id: str
    app_id: str
    business_id: str
    phone: str
    name: str
    tags: list[str]
    fields: dict[str, Any]
    notes: str
    window_open: bool
    assigned_to: str | None
    assigned_name: str | None
    first_seen_at: datetime | None
    last_inbound_at: datetime | None
    messages_in: int
    messages_out: int


class ContactCardPatch(BaseModel):
    name: str | None = Field(default=None, max_length=128)
    tags: list[str] | None = None
    notes: str | None = Field(default=None, max_length=4000)


@router.get("", response_model=ConversationListOut)
async def list_conversations(
    app_id: str | None = None,
    q: str | None = None,
    only_unread: bool = False,
    limit: int = 50,
    offset: int = 0,
    scope: PanelScope = Depends(get_chat_scope),
    session: AsyncSession = Depends(get_session),
) -> ConversationListOut:
    """La lista de la bandeja: una fila por contacto con su ultimo mensaje.

    `q` busca por nombre, telefono o texto del ultimo mensaje (la busqueda
    de una bandeja real es "la señora que preguntó por acrílicas", no un
    id). `unread_total` cuenta TODO el scope, no la pagina: es el numero
    que dice si hay alguien esperando respuesta.
    """
    # El ultimo mensaje de cada contacto, en una pasada: max(created_at)
    # por contacto y luego las filas de esos maximos.
    last = (
        select(
            ChatMessage.contact_id,
            func.max(ChatMessage.created_at).label("last_at"),
        )
        .group_by(ChatMessage.contact_id)
        .subquery()
    )
    rows = (
        await session.execute(
            select(ChatMessage, Contact)
            .join(last, (ChatMessage.contact_id == last.c.contact_id) & (ChatMessage.created_at == last.c.last_at))
            .join(Contact, Contact.id == ChatMessage.contact_id)
            .order_by(ChatMessage.created_at.desc())
            .limit(MAX_CONVERSATIONS_SCANNED)
        )
    ).all()

    # Quien atiende cada hilo, en una sola consulta (evita N+1 al pintar).
    names = {
        row.id: (row.full_name or row.email)
        for row in (await session.execute(select(PanelUser))).scalars()
    }

    windows = WindowChecker(session)
    needle = _fold(q or "")
    matched: list[ConversationOut] = []
    unread_total = 0
    seen: set[str] = set()

    for message, contact in rows:
        if not scope.allows_contact(contact.app_id, contact.business_id):
            continue
        if app_id and contact.app_id != app_id:
            continue
        if contact.id in seen:  # empate exacto de created_at
            continue
        seen.add(contact.id)

        # No leido = entro algo despues de la ultima vez que alguien abrio
        # el hilo. Lo que sale del panel no cuenta: responder ES leer.
        sin_ver = contact.last_read_at is None or contact.last_read_at < message.created_at
        # Una respuesta que Meta rechazo no es una respuesta: la clienta
        # sigue esperando, igual que si nadie hubiera escrito.
        fallo = message.direction == "out" and message.status == "failed"
        unread = (message.direction == "in" or fallo) and sin_ver
        if unread:
            unread_total += 1

        if needle and not (
            needle in _fold(contact.name)
            or needle in _fold(contact.phone)
            or needle in _fold(message.body)
        ):
            continue
        if only_unread and not unread:
            continue

        matched.append(
            ConversationOut(
                contact_id=contact.id,
                app_id=contact.app_id,
                business_id=contact.business_id,
                phone=contact.phone,
                name=contact.name,
                last_body=message.body[:120],
                last_direction=message.direction,
                last_at=message.created_at,
                window_open=await windows.is_open(contact),
                unread=unread,
                last_failed=fallo,
                assigned_to=contact.assigned_to,
                assigned_name=names.get(contact.assigned_to or ""),
            )
        )

    page = matched[offset : offset + min(limit, 200)]
    return ConversationListOut(
        items=page,
        unread_total=unread_total,
        has_more=len(matched) > offset + len(page),
    )


async def _contact_card(session: AsyncSession, contact: Contact) -> ContactCardOut:
    counts = dict(
        (
            await session.execute(
                select(ChatMessage.direction, func.count())
                .where(ChatMessage.contact_id == contact.id)
                .group_by(ChatMessage.direction)
            )
        ).all()
    )
    first = (
        await session.execute(
            select(func.min(ChatMessage.created_at)).where(ChatMessage.contact_id == contact.id)
        )
    ).scalar()

    assigned_name: str | None = None
    if contact.assigned_to:
        user = await session.get(PanelUser, contact.assigned_to)
        assigned_name = (user.full_name or user.email) if user else None

    return ContactCardOut(
        contact_id=contact.id,
        app_id=contact.app_id,
        business_id=contact.business_id,
        phone=contact.phone,
        name=contact.name,
        tags=list(contact.tags),
        fields=dict(contact.fields),
        notes=contact.notes,
        window_open=await WindowChecker(session).is_open(contact),
        assigned_to=contact.assigned_to,
        assigned_name=assigned_name,
        first_seen_at=first,
        last_inbound_at=contact.last_inbound_at,
        messages_in=int(counts.get("in", 0)),
        messages_out=int(counts.get("out", 0)),
    )


@router.get("/{contact_id}", response_model=ContactCardOut)
async def contact_card(
    contact_id: str,
    scope: PanelScope = Depends(get_chat_scope),
    session: AsyncSession = Depends(get_session),
) -> ContactCardOut:
    return await _contact_card(session, await _contact_in_scope(session, contact_id, scope))


@router.patch("/{contact_id}", response_model=ContactCardOut)
async def update_contact_card(
    contact_id: str,
    payload: ContactCardPatch,
    background_tasks: BackgroundTasks,
    scope: PanelScope = Depends(get_chat_scope),
    session: AsyncSession = Depends(get_session),
) -> ContactCardOut:
    contact = await _contact_in_scope(session, contact_id, scope)

    if payload.name is not None and payload.name.strip() and payload.name.strip() != contact.name:
        contact.name = payload.name.strip()
        # La ficha es de la app duena: que se entere del nombre corregido.
        background_tasks.add_task(
            post_contact_renamed, contact.app_id, contact.business_id, contact.phone, contact.name
        )
    if payload.tags is not None:
        # Sin duplicados y sin vacios: una lista de tags con "vip" dos veces
        # convierte cualquier filtro en un resultado raro.
        contact.tags = list(dict.fromkeys(t.strip() for t in payload.tags if t.strip()))
    if payload.notes is not None:
        contact.notes = payload.notes

    await session.commit()
    return await _contact_card(session, contact)


@router.post("/{contact_id}/read", status_code=status.HTTP_204_NO_CONTENT)
async def mark_read(
    contact_id: str,
    scope: PanelScope = Depends(get_chat_scope),
    session: AsyncSession = Depends(get_session),
) -> None:
    """Marca el hilo como leido hasta ahora. Lo llama el panel al abrirlo."""
    contact = await _contact_in_scope(session, contact_id, scope)
    contact.last_read_at = datetime.utcnow()
    await session.commit()


@router.post("/{contact_id}/assign", response_model=ConversationOut)
async def assign(
    contact_id: str,
    payload: AssignIn,
    scope: PanelScope = Depends(get_chat_scope),
    session: AsyncSession = Depends(get_session),
) -> ConversationOut:
    """Quien se hace cargo del hilo. No bloquea a nadie (un candado en una
    bandeja chica estorba mas de lo que ayuda): es una señal para que dos
    personas no contesten lo mismo."""
    contact = await _contact_in_scope(session, contact_id, scope)

    assigned_name: str | None = None
    if payload.user_id is not None:
        user = await session.get(PanelUser, payload.user_id)
        if user is None:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND, detail="Ese usuario no existe."
            )
        assigned_name = user.full_name or user.email
    contact.assigned_to = payload.user_id
    await session.commit()

    last = (
        await session.execute(
            select(ChatMessage)
            .where(ChatMessage.contact_id == contact.id)
            .order_by(ChatMessage.created_at.desc())
            .limit(1)
        )
    ).scalars().first()
    return ConversationOut(
        contact_id=contact.id,
        app_id=contact.app_id,
        business_id=contact.business_id,
        phone=contact.phone,
        name=contact.name,
        last_body=last.body[:120] if last else "",
        last_direction=last.direction if last else "in",
        last_at=last.created_at if last else contact.created_at,
        window_open=await WindowChecker(session).is_open(contact),
        unread=False,
        assigned_to=contact.assigned_to,
        assigned_name=assigned_name,
    )


async def _contact_in_scope(
    session: AsyncSession, contact_id: str, scope: PanelScope
) -> Contact:
    contact = await session.get(Contact, contact_id)
    if contact is None or not scope.allows_contact(contact.app_id, contact.business_id):
        # 404 tambien para lo ajeno: no filtrar existencia entre clientes.
        # Y por NEGOCIO ademas de por app, porque la bandeja embebida en
        # el panel de un salon la mira ese salon, no el dueno de la app:
        # sin esto, un id de contacto adivinado abriria la conversacion
        # de otro negocio.
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Conversacion no encontrada.")
    return contact


@router.get("/{contact_id}/messages", response_model=list[ChatMessageOut])
async def list_messages(
    contact_id: str,
    limit: int = 100,
    scope: PanelScope = Depends(get_chat_scope),
    session: AsyncSession = Depends(get_session),
) -> list[ChatMessageOut]:
    contact = await _contact_in_scope(session, contact_id, scope)
    rows = (
        await session.execute(
            select(ChatMessage)
            .where(ChatMessage.contact_id == contact.id)
            .order_by(ChatMessage.created_at.desc())
            .limit(min(limit, 300))
        )
    ).scalars().all()
    armadas = await _plantillas_armadas(session, contact.app_id, rows)

    return [
        ChatMessageOut(
            id=m.id,
            direction=m.direction,
            message_type=m.message_type,
            body=armadas[m.id][0] if m.id in armadas else m.body,
            payload=({**(m.payload or {}), "buttons": armadas[m.id][1]} if m.id in armadas else m.payload),
            status=m.status,
            origin=m.origin,
            created_at=m.created_at,
        )
        for m in reversed(rows)
    ]


async def _plantillas_armadas(
    session: AsyncSession, app_id: str, rows: list[ChatMessage]
) -> dict[str, tuple[str, list[dict[str, str]]]]:
    """El texto y los botones de cada plantilla del hilo, como los recibio
    la persona. Se arma al MOSTRAR y no al guardar, para que tambien se lean
    bien los mensajes que ya estaban (ver render_template_bubble).

    La plantilla se busca en la cuenta ACTUAL de la app antes que en
    cualquier otra: la misma plantilla puede existir con otro texto en la
    cuenta de pruebas. Si no esta en el espejo, se deja el resumen de
    siempre -- peor seria inventar un texto.
    """
    con_plantilla = [m for m in rows if (m.payload or {}).get("template")]
    if not con_plantilla:
        return {}

    identidad = await resolve_by_app_id(session, app_id)
    cuenta_actual = identidad.whatsapp.waba_id if identidad and identidad.whatsapp else None

    nombres = {str(m.payload["template"]) for m in con_plantilla}
    plantillas = (
        await session.execute(
            select(WhatsAppTemplate).where(
                WhatsAppTemplate.app_id == app_id, WhatsAppTemplate.name.in_(nombres)
            )
        )
    ).scalars().all()

    def elegir(nombre: str, idioma: str | None) -> WhatsAppTemplate | None:
        candidatas = [
            tpl for tpl in plantillas if tpl.name == nombre and (idioma is None or tpl.language == idioma)
        ]
        # La sincronizada más reciente primero, y encima de todo la de la
        # cuenta actual. Dos pasadas porque `sort` es estable: la segunda
        # respeta el orden de la primera entre las que empatan.
        candidatas.sort(key=lambda tpl: tpl.last_synced_at or datetime.min, reverse=True)
        candidatas.sort(key=lambda tpl: tpl.waba_id != cuenta_actual)
        return candidatas[0] if candidatas else None

    armadas: dict[str, tuple[str, list[dict[str, str]]]] = {}
    for m in con_plantilla:
        payload = m.payload or {}
        tpl = elegir(str(payload["template"]), payload.get("language"))
        if tpl is None or not tpl.components:
            continue

        valores = payload.get("template_params")
        if valores is None:
            # Mensajes de antes de guardar los valores: se recuperan del
            # resumen "[plantilla x] a · b · c".
            resumen = (m.body or "").split("] ", 1)[-1] if (m.body or "").startswith("[plantilla") else ""
            valores = resumen.split(" · ") if resumen else []

        armadas[m.id] = render_template_bubble(
            tpl.components, [str(v) for v in valores], payload.get("template_header_params") or []
        )
    return armadas


@router.post(
    "/{contact_id}/messages", response_model=ChatMessageOut, status_code=status.HTTP_201_CREATED
)
async def send_message(
    contact_id: str,
    payload: ChatSendIn,
    background_tasks: BackgroundTasks,
    scope: PanelScope = Depends(get_chat_scope),
    session: AsyncSession = Depends(get_session),
) -> ChatMessageOut:
    """Responde desde la bandeja, como si el negocio escribiera desde el
    celular que no tiene.

    Dos formas, segun la ventana de 24h: `text` (libre, solo entrega
    dentro de la ventana) o `template` (plantilla aprobada - lo UNICO que
    entrega fuera, y la razon por la que el panel puede rescatar una
    conversacion fria sin depender de un flujo).

    Al terminar avisa a la app duena (`human_reply`) para que SU bot se
    calle: el saliente del panel sale por Cloud API y la app no se
    enteraria de otra forma."""
    contact = await _contact_in_scope(session, contact_id, scope)

    app = await resolve_by_app_id(session, contact.app_id)
    if app is None or app.whatsapp is None:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=f"La app '{contact.app_id}' no tiene WhatsApp configurado.",
        )

    identity, _ = await resolve_whatsapp_identity(
        session, app, contact.business_id or contact.app_id
    )

    if payload.template is not None:
        await _assert_template_sendable(session, contact.app_id, identity, payload.template)
        message_out = OutboundMessage(
            to=contact.phone,
            # Reabrir la conversacion se cobra: es utility, no service.
            category="utility",
            template_name=payload.template.name,
            template_language=payload.template.language,
            template_components=[
                {
                    "type": "body",
                    "parameters": [{"type": "text", "text": p} for p in payload.template.params],
                }
            ]
            if payload.template.params
            else [],
        )
    elif payload.media is not None:
        message_out = OutboundMessage(
            to=contact.phone,
            category="service",
            media_kind=payload.media.kind,
            media_url=payload.media.url,
            media_caption=payload.media.caption,
            media_filename=payload.media.filename,
        )
    else:
        message_out = OutboundMessage(to=contact.phone, text=payload.text, category="service")

    sender = get_channel_registry().resolve("whatsapp")
    result = await sender.send(identity, message_out)

    message = log_outbound_chat(
        session, contact=contact, message=message_out, result=result, origin="panel"
    )
    # Contestar ES leer: si no, el hilo que acabas de atender sigue
    # apareciendo en negrita como pendiente.
    contact.last_read_at = datetime.utcnow()
    await session.commit()

    background_tasks.add_task(
        post_app_event,
        app.whatsapp,
        "human_reply",
        {
            "business_id": contact.business_id,
            "contact": {
                "name": contact.name,
                "phone": contact.phone,
                "tags": contact.tags,
                "fields": contact.fields,
            },
            "message": {
                "type": "template" if payload.template else "text",
                "text": message.body,
                "template": payload.template.name if payload.template else None,
            },
            "status": result.status,
        },
    )

    return ChatMessageOut(
        id=message.id,
        direction=message.direction,
        message_type=message.message_type,
        body=message.body,
        payload=message.payload,
        status=message.status,
        origin=message.origin,
        created_at=message.created_at,
    )


async def _assert_template_sendable(
    session: AsyncSession, app_id: str, identity, template: ChatTemplateIn
) -> None:
    """Corta antes de llamar a Meta si el espejo sabe que la plantilla no
    esta aprobada: un rechazo seguro no vale gastar rate limit, y el
    operador merece el motivo en pantalla. Si el espejo no la conoce se
    envia igual (el espejo es opt-in, no un registro obligatorio)."""
    waba_id = identity.whatsapp.waba_id if identity.whatsapp else None
    row = await TemplateRepository(session).get_for_send(
        app_id, waba_id, template.name, template.language
    )
    if row is None or row.status == "APPROVED":
        return
    detail = f" ({row.reason})" if row.reason else ""
    raise HTTPException(
        status_code=status.HTTP_409_CONFLICT,
        detail=(
            f"La plantilla '{row.name}' ({row.language}) esta en estado {row.status}{detail} - "
            "Meta la rechazaria. Usa otra o corrigela en Plantillas."
        ),
    )
