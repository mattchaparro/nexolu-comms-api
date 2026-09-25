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
