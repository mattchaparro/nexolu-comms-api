"""Lo que la app duena sabe del contacto y Connect deberia mostrar.

Por que existe. La ficha de la clienta vive en la app (el Spa): ahi se
corrige el nombre, o la clienta misma lo confirma por WhatsApp. El chat de
Connect, en cambio, mostraba el nombre del perfil de WhatsApp ("." o un
emoji), y quien contestaba no sabia con quien hablaba. La app lo manda
aca, de servidor a servidor, con su API key.

No devuelve el eco: un cambio que llega por aca NO dispara
`contact_updated` hacia la app (ese evento es solo para ediciones hechas
en el panel), asi que no hay ida y vuelta infinita.
"""
from __future__ import annotations

import re
from typing import Any

from fastapi import APIRouter, Depends
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from nexolu_comms_api.core.auth.apps import AppIdentity
from nexolu_comms_api.core.auth.dependencies import get_current_app
from nexolu_comms_api.core.db.entities import Contact
from nexolu_comms_api.core.db.session import get_session

router = APIRouter(prefix="/v1/contacts", tags=["contacts"])


class ContactNameIn(BaseModel):
    phone: str = Field(min_length=6, max_length=32)
    business_id: str = Field(default="", max_length=64)
    name: str = Field(min_length=1, max_length=128)


class ContactNameOut(BaseModel):
    updated: int


@router.patch("", response_model=ContactNameOut)
async def update_contact_name(
    payload: ContactNameIn,
    app: AppIdentity = Depends(get_current_app),
    session: AsyncSession = Depends(get_session),
) -> ContactNameOut:
    """Pone el nombre en los contactos de ESTA app con ese telefono: el del
    negocio y, si existe, el de la app sin negocio (numero compartido). Si
    no hay contacto no crea ninguno -- 0 y listo: Connect no tiene por que
    conocer a quien nunca escribio."""
    phone = re.sub(r"\D", "", payload.phone)
    name = payload.name.strip()
    businesses = {payload.business_id, ""}

    contacts = (
        await session.execute(
            select(Contact).where(
                Contact.app_id == app.app_id,
                Contact.phone == phone,
                Contact.business_id.in_(businesses),
            )
        )
    ).scalars().all()

    updated = 0
    for contact in contacts:
        if contact.name != name:
            contact.name = name
            updated += 1
    await session.commit()
    return ContactNameOut(updated=updated)


class ContactSyncItem(BaseModel):
    phone: str = Field(min_length=6, max_length=32)
    name: str = Field(default="", max_length=128)
    # Se MEZCLAN con lo que ya tiene el contacto: la app manda lo suyo
    # (acepta_promociones, ultima_visita...) sin pisar lo que pusieron los
    # flujos. Un valor null borra ese campo.
    fields: dict[str, Any] = Field(default_factory=dict)
    tags_add: list[str] = Field(default_factory=list)
    tags_remove: list[str] = Field(default_factory=list)


class ContactSyncIn(BaseModel):
    business_id: str = Field(default="", max_length=64)
    contacts: list[ContactSyncItem] = Field(max_length=500)


class ContactSyncOut(BaseModel):
    created: int
    updated: int


@router.put("/bulk", response_model=ContactSyncOut)
async def sync_contacts(
    payload: ContactSyncIn,
    app: AppIdentity = Depends(get_current_app),
    session: AsyncSession = Depends(get_session),
) -> ContactSyncOut:
    """La app duena publica lo que sabe de sus clientes, para que las
    difusiones de Connect puedan filtrar por eso (ver core/broadcasts.py).

    A diferencia del PATCH de nombre, este SI crea el contacto: para
    escribirle a una clienta que nunca le ha escrito a este numero hay que
    tenerla. No aparece en la bandeja (la bandeja lista conversaciones, no
    contactos) hasta que haya un mensaje.

    `fields.negocio` queda con el negocio que lo publico: en un numero
    compartido el contacto puede estar guardado sin negocio (""), y la
    difusion de un salon tiene que saber que esta clienta es suya.
    """
    from nexolu_comms_api.core.flows.engine import ContactRepository

    repo = ContactRepository(session)
    created = updated = 0
    for item in payload.contacts:
        phone = re.sub(r"\D", "", item.phone)
        if len(phone) < 8:
            continue
        before = await session.execute(
            select(Contact.id).where(Contact.app_id == app.app_id, Contact.phone == phone).limit(1)
        )
        is_new = before.first() is None
        contact = await repo.get_or_create(app.app_id, payload.business_id, phone, item.name.strip())

        fields = dict(contact.fields or {})
        for key, value in item.fields.items():
            if value is None:
                fields.pop(key, None)
            else:
                fields[key] = value
        if payload.business_id:
            fields["negocio"] = payload.business_id
        tags = [t for t in (contact.tags or []) if t not in item.tags_remove]
        tags += [t for t in item.tags_add if t not in tags]

        name = item.name.strip()
        changed = fields != (contact.fields or {}) or tags != (contact.tags or []) or (name and name != contact.name)
        contact.fields = fields
        contact.tags = tags
        if name:
            contact.name = name
        if is_new:
            created += 1
        elif changed:
            updated += 1
        await session.flush()

    await session.commit()
    return ContactSyncOut(created=created, updated=updated)
