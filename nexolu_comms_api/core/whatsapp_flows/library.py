"""Biblioteca de formularios listos: el punto de partida de "Crear desde
plantilla" en el panel y de la auto-provision al conectar un canal.

Cada entrada es un Flow JSON ya aceptado por el validador de Meta. El de
`confirm_booking` es el del spa (nexolu-spa-api/docs/whatsapp-flows/
confirmar-cita.json): su payload ES un contrato con `App\\Ai\\BookingForm`
(`"pedido": "cita"`, servicio, fecha, hora, nombre, para_quien) - no
cambiar esas claves sin tocar el spa.

Vive como dict de Python y no como .json suelto para que viaje dentro del
paquete sin configurar package-data.
"""
from __future__ import annotations

import copy
from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True)
class LibraryEntry:
    key: str
    name: str  # nombre del Flow en Meta (unico por WABA)
    title: str  # lo que ve el operador en el panel
    description: str
    categories: tuple[str, ...]
    flow_json: dict[str, Any]


_CONFIRM_BOOKING: dict[str, Any] = {
    "version": "7.2",
    "screens": [
        {
            "id": "CONFIRMAR",
            "title": "Confirma tu cita",
            "terminal": True,
            "data": {
                "resumen": {"type": "string", "__example__": "Semipermanente — jueves 24 de septiembre"},
                "horas": {
                    "type": "array",
                    "items": {
                        "type": "object",
                        "properties": {"id": {"type": "string"}, "title": {"type": "string"}},
                    },
                    "__example__": [
                        {"id": "15:00", "title": "3:00 pm con Anyi"},
                        {"id": "16:30", "title": "4:30 pm con Karen"},
                    ],
                },
                "servicio": {"type": "string", "__example__": "Semipermanente"},
                "fecha": {"type": "string", "__example__": "2026-09-24"},
                "hora": {"type": "string", "__example__": "15:00"},
                "nombre": {"type": "string", "__example__": ""},
            },
            "layout": {
                "type": "SingleColumnLayout",
                "children": [
                    {
                        "type": "Form",
                        "name": "cita",
                        "init-values": {"hora": "${data.hora}", "nombre": "${data.nombre}"},
                        "children": [
                            {"type": "TextHeading", "text": "${data.resumen}"},
                            {
                                "type": "TextBody",
                                "text": "Revisa la hora y tu nombre, y confirma. Si quieres otro día u "
                                "otro servicio, cierra esto y escríbenos.",
                            },
                            {
                                "type": "Dropdown",
                                "name": "hora",
                                "label": "Hora (las que están libres)",
                                "required": True,
                                "data-source": "${data.horas}",
                            },
                            {
                                "type": "TextInput",
                                "name": "nombre",
                                "label": "Tu nombre",
                                "input-type": "text",
                                "required": False,
                            },
                            {
                                "type": "TextInput",
                                "name": "para_quien",
                                "label": "Si la cita es para otra persona, su nombre",
                                "input-type": "text",
                                "required": False,
                            },
                            {
                                "type": "Footer",
                                "label": "Confirmar cita",
                                "on-click-action": {
                                    "name": "complete",
                                    "payload": {
                                        "pedido": "cita",
                                        "servicio": "${data.servicio}",
                                        "fecha": "${data.fecha}",
                                        "hora": "${form.hora}",
                                        "nombre": "${form.nombre}",
                                        "para_quien": "${form.para_quien}",
                                    },
                                },
                            },
                        ],
                    }
                ],
            },
        }
    ],
}

LIBRARY: dict[str, LibraryEntry] = {
    "confirm_booking": LibraryEntry(
        key="confirm_booking",
        name="confirmar_cita",
        title="Confirmar cita",
        description=(
            "Servicio y fecha de solo lectura, hora de una lista con las libres, nombre y "
            "'para quién' opcionales. El payload es el que entiende el agente del spa."
        ),
        categories=("APPOINTMENT_BOOKING",),
        flow_json=_CONFIRM_BOOKING,
    ),
}


def get_entry(key: str) -> LibraryEntry | None:
    return LIBRARY.get(key)


def fresh_json(entry: LibraryEntry) -> dict[str, Any]:
    """Copia profunda: la plantilla nunca se muta desde afuera."""
    return copy.deepcopy(entry.flow_json)
