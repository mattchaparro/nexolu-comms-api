"""Modelos de persistencia del servicio.

Deliberadamente NO hay tabla de negocio aca (sin `productos`, sin `ventas`):
eso vive en la base de datos de cada aplicacion. Lo que este servicio
persiste es el rastro de cada envio (auditoria + la fuente cruda para los
reportes de uso/costo) - el estado que le pertenece al envio de mensajes, no
al negocio.
"""
from __future__ import annotations

import uuid
from datetime import datetime

from sqlalchemy import DateTime, Index, Integer, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from nexolu_comms_api.core.db.session import Base


def _uuid() -> str:
    return uuid.uuid4().hex


class Notification(Base):
    """Un intento de envio por UN canal. Un POST /v1/notifications/send con
    3 canales genera 3 filas - cada canal se registra, factura y falla de
    forma independiente."""

    __tablename__ = "notifications"
    __table_args__ = (
        Index("ix_notifications_tenant", "app_id", "business_id"),
        Index("ix_notifications_reference", "reference"),
        Index("ix_notifications_created_at", "created_at"),
    )

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=_uuid)
    app_id: Mapped[str] = mapped_column(String(64))
    business_id: Mapped[str] = mapped_column(String(64))
    # Identificador libre que la app llamante define para agrupar/rastrear
    # (p.ej. "low_stock_alert:456") - este servicio no le da significado,
    # solo lo guarda y lo permite filtrar.
    reference: Mapped[str | None] = mapped_column(String(191), nullable=True)
    channel: Mapped[str] = mapped_column(String(32))
    recipient: Mapped[str] = mapped_column(String(255))
    status: Mapped[str] = mapped_column(String(16))  # sent | failed | skipped
    provider_message_id: Mapped[str | None] = mapped_column(String(191), nullable=True)
    error: Mapped[str | None] = mapped_column(Text, nullable=True)
    # None cuando el proveedor no informa costo para ese envio (p.ej. email
    # via Brevo) - no es lo mismo que "cost=0". Ver core/telemetry/usage.py.
    cost_micros: Mapped[int | None] = mapped_column(Integer, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)
