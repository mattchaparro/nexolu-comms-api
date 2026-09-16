"""Disparo de flujos POR LA APP duena (auth de app, como el envio).

El caso que motivo todo esto: el Spa agenda una cita y dispara el flujo
`post_agenda` para ese telefono con variables (`fecha`, `servicio`,
`slug`...) - el cliente recibe el mensaje con botones (condiciones de
cancelacion, garantias, gestionar su cita en la web) y el motor atiende
las respuestas. Las variables quedan en el contexto de la sesion y se
interpolan en cada nodo; los tags/fields que el flujo fija quedan en el
contacto, consultables por el panel y por la propia app.
"""
from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from nexolu_comms_api.core.auth.apps import AppIdentity
from nexolu_comms_api.core.auth.dependencies import get_current_app
from nexolu_comms_api.core.db.entities import Flow
from nexolu_comms_api.core.db.session import get_session
from nexolu_comms_api.core.flows.engine import start_flow

router = APIRouter(prefix="/v1/flows", tags=["flows"])


class TriggerFlowIn(BaseModel):
    flow: str = Field(min_length=1, description="Nombre del flujo (unico por app+negocio).")
    to: str = Field(min_length=5, description="Telefono del contacto, formato internacional.")
    business_id: str | None = Field(
        default=None, description="Negocio dentro de la app; omitir para flujos a nivel de app."
    )
    variables: dict[str, Any] = Field(
        default_factory=dict,
        description="Contexto del flujo: se interpola en los nodos como {{variable}}.",
    )
    contact_name: str = Field(default="", description="Nombre del contacto, si la app lo conoce.")


class TriggerFlowOut(BaseModel):
    session_id: str
    status: str  # active (esperando respuesta) | waiting (en un delay) | completed


@router.post("/trigger", response_model=TriggerFlowOut)
async def trigger_flow(
    payload: TriggerFlowIn,
    app: AppIdentity = Depends(get_current_app),
    session: AsyncSession = Depends(get_session),
) -> TriggerFlowOut:
    business_id = payload.business_id or ""
    flow = (
        await session.execute(
            select(Flow).where(
                Flow.app_id == app.app_id,
                Flow.business_id.in_(["", business_id]),
                Flow.name == payload.flow,
            )
        )
    ).scalars().first()

    if flow is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail=f"El flujo '{payload.flow}' no existe."
        )
    if not flow.is_active:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT, detail=f"El flujo '{payload.flow}' esta inactivo."
        )

    flow_session = await start_flow(
        session,
        app,
        flow,
        phone=payload.to,
        business_id=payload.business_id,
        variables=payload.variables,
        contact_name=payload.contact_name,
    )
    return TriggerFlowOut(session_id=flow_session.id, status=flow_session.status)
