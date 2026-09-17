"""El motor de flujos de Connect: la pieza de ManyChat que orquesta la
conversacion.

## El esquema de un flujo (`Flow.definition`)

```json
{
  "start": "menu",
  "nodes": {
    "menu": {
      "type": "buttons",
      "text": "Hola {{contact.name}}, tu cita quedo para {{fecha}}.",
      "buttons": [
        {"id": "cancelacion", "title": "Cancelaciones", "next": "cancelacion"},
        {"id": "gestionar", "title": "Gestionar cita", "next": "gestionar"}
      ]
    },
    "cancelacion": {"type": "message", "text": "Puedes cancelar hasta...", "add_tags": ["pregunto_cancelacion"]},
    "gestionar": {"type": "cta_url", "text": "Gestiona tu cita:", "url": "https://agenda.nexolu.co/{{slug}}", "button": "Abrir agenda"}
  }
}
```

Tipos de nodo: `message` (texto, sigue solo por `next`), `buttons` (hasta 3
- regla de Meta - y ESPERA la respuesta: la sesion queda parada ahi),
`cta_url` (boton que abre un link - la accion de negocio real vive en la
web/app duena, no aca), `condition` (no envia nada: evalua `when` sobre el
contacto/contexto y sigue por `then` o `else`) y `delay` (no envia nada:
la sesion queda `waiting` con `resume_at` y el worker de reanudacion sigue
por `next` cuando vence). Todo nodo puede ademas `add_tags`/`remove_tags`/
`set_fields` sobre el contacto. `{{...}}` interpola contra el contexto de
la sesion (variables del trigger + `contact.*`).

`condition` multi-rama (el else-if de ManyChat): en vez de `when`+`then`,
`"cases": [{"when": {...}, "next": <nodo|null>}, ...]` (1 a 8) - se
evaluan EN ORDEN y gana el primer caso que matchee; si ninguno, sigue por
`else`. La forma clasica `when`+`then`/`else` sigue valiendo.

`condition.when` (y el `when` de cada caso) acepta exactamente UNA de
estas formas:
  {"tag": "vip"} / {"not_tag": "vip"}          - tiene / no tiene el tag
  {"field": "x", "equals": "y"}                - tambien not_equals, contains
  {"field": "x", "exists": true}               - tiene valor no vacio
`field` es una ruta del contexto (`fecha`, `contact.name`...); un nombre
simple que no exista ahi se busca en `contact.fields` (los custom fields).

`delay` lleva `minutes` (1 a 20160 = 14 dias). ManyChat-semantica: un
mensaje entrante durante el delay NO lo interrumpe (la conversacion es de
la app), pero un flujo nuevo del mismo contacto si lo reemplaza.

`random` (el aleatorizador A/B): no envia nada, tira un dado ponderado y
sigue por la rama elegida. `branches` = lista de 2 a 5 objetos
`{"weight": <entero>=1>, "next": <nodo o null>}`; se re-tira en cada
corrida (un split pegajoso por contacto se logra con un tag).

Los bloques de contenido de Meta (paridad con el modulo de mensaje de
ManyChat):
  `media`   {"kind": "image|video|audio|document", "url": <link publico>,
             "caption"?, "filename"?, "next"?} - Meta descarga el archivo.
  `list`    {"text", "button"?, "rows": [{"id","title","description"?,
             "next"?}] (1-10)} - menu interactive.list; ESPERA la eleccion
             como `buttons` (list_reply o el titulo escrito).
  `capture` {"text", "field", "next"?} - pregunta y ESPERA: el siguiente
             texto libre del contacto queda en contact.fields[field]
             (la "Recopilacion de datos" de ManyChat). Un mensaje sin
             texto no cuenta; sigue esperando.
  `template` {"template": <nombre aprobado>, "language"?: "es",
             "params"?: [texto por {{1}}, {{2}}...], "next"?} - envia una
             plantilla aprobada de Meta. Es la UNICA pieza que entrega
             fuera de la ventana de 24h: el seguimiento correcto despues
             de un `delay` largo. Se cobra como utility.
  `blocks`  el paso "Enviar mensaje" de ManyChat: {"blocks": [...],
             "next"?} - una PILA de 1-10 bloques enviados seguidos, cada
             uno su propio mensaje de WhatsApp:
               {"type":"text","text",...,"buttons"?: [max 3 c/u]}
               {"type":"cta","text","url","button"?}
               {"type":"image|video|audio|document","url","caption"?,"filename"?}
               {"type":"wait","seconds":1-15}  (pausa corta entre textos)
               {"type":"capture","text","field","next"?}  (ULTIMO; sin botones en el nodo)
               {"type":"list","text","button"?,"rows":[...]}  (ULTIMO)
             Ids de opcion unicos por nodo. Si algun bloque tiene botones
             o el ultimo es list/capture, el nodo ESPERA; si no, sigue
             por `next`.
  `actions` el "Realiza las siguientes acciones..." de ManyChat, con el
             guardrail de Connect: {"actions": [...], "next"?} - 1 a 10
             acciones en orden, sin enviar mensajes:
               {"type":"add_tags"|"remove_tags","tags":[...]}
               {"type":"set_fields","fields":{k:v interpolable}}
               {"type":"clear_fields","fields":[k...]}
               {"type":"http_request","method":"GET|POST","url",
                "headers"?,"body"?,"save"?:{campo:"ruta.de.respuesta"}}
                 - la "Solicitud externa": pega a la API del negocio y
                   guarda partes de la respuesta en custom fields.
                   Fail-soft: API caida = log, el flujo sigue.
               {"type":"notify_app","message","emails"?:[...]} - evento
                 flow_notify FIRMADO al callback de la app duena, y
                 correo directo a esos admins/agentes ("pidio un humano"
                 no puede depender de que alguien mire un panel).
               {"type":"start_flow","flow"} - salta a otro flujo de la
                 app (ULTIMA accion; hereda variables; supersede esta
                 sesion). El goto que evita arboles gigantes.
  `product` {"retailer_id": <b{negocio}-{sku}>, "text"?, "next"?} - UN
             producto del catalogo (SPM); o {"header"?, "sections":
             [{"title", "retailer_ids": [...]}]} - varios (MPM, max 10
             secciones / 30 productos). El catalog_id sale de la
             identidad efectiva (app o canal del negocio); el pedido que
             la clienta arme vuelve por el webhook `order` de siempre.

La clave raiz `ui` (posiciones del builder visual del panel) se guarda con
la definicion y el motor la ignora por completo.

## Las dos entradas

- `start_flow()`: lo dispara la app por API (POST /v1/flows/trigger) o un
  keyword entrante. Corre nodos encadenados hasta parar en un `buttons`
  (sesion `active`) o agotarse (sesion `completed`).
- `handle_inbound()`: cada `message` entrante del webhook pasa por aca
  DESPUES de persistirse y sin afectar el reenvio a la app duena (el motor
  es una capa adicional, no un secuestro del canal). Si el contacto tiene
  una sesion esperando en botones, la respuesta avanza el flujo; si no,
  se evaluan los keywords de los flujos activos. Un texto que no matchea
  nada no genera respuesta: esa conversacion es de la app, no del motor.

Guardas: tope de nodos por corrida (anti-loop), sesiones con mas de 24h
quietas expiran, y un flujo nuevo reemplaza (`superseded`) la sesion
anterior del contacto - el flujo mas reciente gana, como en ManyChat.
"""
from __future__ import annotations

import asyncio
import ipaddress
import json
import logging
import random
import re
from datetime import datetime, timedelta
from typing import Any
from urllib.parse import urlparse

import httpx
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from nexolu_comms_api.core.auth.apps import AppIdentity, resolve_by_app_id
from nexolu_comms_api.core.channels.base import OutboundMessage
from nexolu_comms_api.core.channels.business_channels import resolve_whatsapp_identity
from nexolu_comms_api.core.channels.registry import get_channel_registry
from nexolu_comms_api.core.db.entities import ChatMessage, Contact, Flow, FlowSession, WebhookEvent
from nexolu_comms_api.core.db.repository import NotificationRepository
from nexolu_comms_api.core.db.session import get_sessionmaker
from nexolu_comms_api.core.webhooks.signing import build_forward_headers

logger = logging.getLogger(__name__)

# Tope de nodos ejecutados en UNA corrida: un flujo legitimo manda 2-4
# mensajes seguidos; 20 solo se alcanza con un ciclo mal armado.
MAX_NODES_PER_RUN = 20

SESSION_TTL_HOURS = 24

VALID_NODE_TYPES = (
    "message",
    "buttons",
    "cta_url",
    "condition",
    "delay",
    "random",
    "media",
    "list",
    "capture",
    "template",
    "product",
    "blocks",
    "actions",
)

# El nodo Acciones (paridad con "Realiza las siguientes acciones..." de
# ManyChat, filtrado por el guardrail de Connect: la accion de NEGOCIO
# vive en la app duena - por eso notify_app y http_request en vez de
# tocar inventario/citas desde aca).
ACTION_TYPES = (
    "add_tags",
    "remove_tags",
    "set_fields",
    "clear_fields",
    "http_request",
    "notify_app",
    "start_flow",
)
MAX_ACTIONS_PER_NODE = 10
HTTP_ACTION_TIMEOUT_SECONDS = 6  # una API lenta no puede colgar el flujo

# El paso "Enviar mensaje" de ManyChat: UN nodo `blocks` = una PILA de
# bloques de contenido que se envian seguidos (texto+botones, multimedia,
# retraso corto, cta, y al final una lista o una captura que esperan).
BLOCK_TYPES = ("text", "cta", "image", "video", "audio", "document", "wait", "capture", "list")
MAX_BLOCKS_PER_NODE = 10
MAX_BLOCK_WAIT_SECONDS = 15  # retraso CORTO entre textos; lo largo es el nodo delay

# Reglas de Meta para el MPM (multi-product message).
MAX_PRODUCT_SECTIONS = 10
MAX_PRODUCTS_TOTAL = 30

# Nodos que envian un mensaje CON texto obligatorio ('media' tambien envia,
# pero su contenido es el archivo y el caption es opcional).
SENDING_NODE_TYPES = ("message", "buttons", "cta_url", "list", "capture")

# Nodos que dejan la sesion esperando la respuesta del contacto.
WAITING_NODE_TYPES = ("buttons", "list", "capture")

MEDIA_KINDS = ("image", "video", "audio", "document")

MAX_LIST_ROWS = 10  # regla de Meta para interactive.list

MAX_DELAY_MINUTES = 20160  # 14 dias: mas alla, es una campana, no un flujo.

# Aleatorizador: entre 2 y 5 ramas (regla practica de ManyChat; mas ramas
# es senal de que el flujo necesita una condicion, no un dado).
MAX_RANDOM_BRANCHES = 5

# Condicion multi-rama (else-if de ManyChat): tope sano de casos.
MAX_CONDITION_CASES = 8

_CONDITION_OPS = ("equals", "not_equals", "contains", "exists")


class FlowDefinitionError(ValueError):
    """La definicion no es ejecutable; el mensaje dice exactamente por que."""


def validate_definition(definition: dict[str, Any]) -> None:
    """Valida al guardar, no al ejecutar: un flujo roto debe rebotar en el
    panel con un mensaje claro, nunca descubrirse con un cliente en vivo."""
    nodes = definition.get("nodes")
    start = definition.get("start")
    if not isinstance(nodes, dict) or not nodes:
        raise FlowDefinitionError("La definicion necesita 'nodes' con al menos un nodo.")
    if not start or start not in nodes:
        raise FlowDefinitionError(f"'start' debe apuntar a un nodo existente (vino: {start!r}).")

    for node_id, node in nodes.items():
        if not isinstance(node, dict):
            raise FlowDefinitionError(f"El nodo '{node_id}' no es un objeto.")
        node_type = node.get("type")
        if node_type not in VALID_NODE_TYPES:
            raise FlowDefinitionError(
                f"El nodo '{node_id}' tiene type {node_type!r}; validos: {', '.join(VALID_NODE_TYPES)}."
            )
        if node_type in SENDING_NODE_TYPES and not node.get("text"):
            raise FlowDefinitionError(f"El nodo '{node_id}' necesita 'text'.")
        if node.get("next") is not None and node["next"] not in nodes:
            raise FlowDefinitionError(f"El nodo '{node_id}' apunta a 'next' inexistente: {node['next']!r}.")

        if node_type == "buttons":
            buttons = node.get("buttons")
            if not isinstance(buttons, list) or not (1 <= len(buttons) <= 3):
                raise FlowDefinitionError(
                    f"El nodo '{node_id}' necesita entre 1 y 3 'buttons' (regla de Meta)."
                )
            for button in buttons:
                if not (isinstance(button, dict) and button.get("id") and button.get("title")):
                    raise FlowDefinitionError(f"Un boton de '{node_id}' necesita 'id' y 'title'.")
                if button.get("next") is not None and button["next"] not in nodes:
                    raise FlowDefinitionError(
                        f"El boton '{button.get('id')}' de '{node_id}' apunta a nodo inexistente."
                    )
        if node_type == "cta_url" and not node.get("url"):
            raise FlowDefinitionError(f"El nodo '{node_id}' (cta_url) necesita 'url'.")

        if node_type == "condition":
            _validate_condition(node_id, node, nodes)

        if node_type == "delay":
            minutes = node.get("minutes")
            if not isinstance(minutes, int) or not (1 <= minutes <= MAX_DELAY_MINUTES):
                raise FlowDefinitionError(
                    f"El nodo '{node_id}' (delay) necesita 'minutes' entero entre 1 y {MAX_DELAY_MINUTES}."
                )

        if node_type == "media":
            if node.get("kind") not in MEDIA_KINDS:
                raise FlowDefinitionError(
                    f"El nodo '{node_id}' (media) necesita 'kind' en: {', '.join(MEDIA_KINDS)}."
                )
            if not node.get("url"):
                raise FlowDefinitionError(f"El nodo '{node_id}' (media) necesita 'url' publica.")

        if node_type == "list":
            rows = node.get("rows")
            if not isinstance(rows, list) or not (1 <= len(rows) <= MAX_LIST_ROWS):
                raise FlowDefinitionError(
                    f"El nodo '{node_id}' (list) necesita entre 1 y {MAX_LIST_ROWS} 'rows' (regla de Meta)."
                )
            for row in rows:
                if not (isinstance(row, dict) and row.get("id") and row.get("title")):
                    raise FlowDefinitionError(f"Una fila de '{node_id}' necesita 'id' y 'title'.")
                if row.get("next") is not None and row["next"] not in nodes:
                    raise FlowDefinitionError(
                        f"La fila '{row.get('id')}' de '{node_id}' apunta a nodo inexistente."
                    )

        if node_type == "capture" and not node.get("field"):
            raise FlowDefinitionError(
                f"El nodo '{node_id}' (capture) necesita 'field': el custom field donde guardar la respuesta."
            )

        if node_type == "blocks":
            _validate_blocks(node_id, node, nodes)

        if node_type == "actions":
            _validate_actions(node_id, node)

        if node_type == "product":
            sections = node.get("sections")
            if not node.get("retailer_id") and not sections:
                raise FlowDefinitionError(
                    f"El nodo '{node_id}' (product) necesita 'retailer_id' (un producto) "
                    "o 'sections' (varios)."
                )
            if sections is not None:
                if not isinstance(sections, list) or not (
                    1 <= len(sections) <= MAX_PRODUCT_SECTIONS
                ):
                    raise FlowDefinitionError(
                        f"Las 'sections' de '{node_id}' van de 1 a {MAX_PRODUCT_SECTIONS} (regla de Meta)."
                    )
                total = 0
                for section in sections:
                    ids = section.get("retailer_ids") if isinstance(section, dict) else None
                    if not (isinstance(section, dict) and section.get("title") and isinstance(ids, list) and ids):
                        raise FlowDefinitionError(
                            f"Cada seccion de '{node_id}' necesita 'title' y 'retailer_ids' no vacios."
                        )
                    total += len(ids)
                if total > MAX_PRODUCTS_TOTAL:
                    raise FlowDefinitionError(
                        f"'{node_id}' lista {total} productos; Meta permite maximo {MAX_PRODUCTS_TOTAL}."
                    )

        if node_type == "template":
            if not node.get("template"):
                raise FlowDefinitionError(
                    f"El nodo '{node_id}' (template) necesita 'template': el nombre de la plantilla aprobada."
                )
            params = node.get("params")
            if params is not None and not (
                isinstance(params, list) and all(isinstance(p, str) for p in params)
            ):
                raise FlowDefinitionError(
                    f"Los 'params' de '{node_id}' deben ser una lista de textos (uno por {{{{1}}}}, {{{{2}}}}...)."
                )

        if node_type == "random":
            branches = node.get("branches")
            if not isinstance(branches, list) or not (2 <= len(branches) <= MAX_RANDOM_BRANCHES):
                raise FlowDefinitionError(
                    f"El nodo '{node_id}' (random) necesita entre 2 y {MAX_RANDOM_BRANCHES} 'branches'."
                )
            for index, branch in enumerate(branches):
                if not isinstance(branch, dict):
                    raise FlowDefinitionError(f"La rama {index} de '{node_id}' no es un objeto.")
                weight = branch.get("weight")
                if not isinstance(weight, int) or weight < 1:
                    raise FlowDefinitionError(
                        f"La rama {index} de '{node_id}' necesita 'weight' entero >= 1."
                    )
                if branch.get("next") is not None and branch["next"] not in nodes:
                    raise FlowDefinitionError(
                        f"La rama {index} de '{node_id}' apunta a nodo inexistente: {branch['next']!r}."
                    )


def _validate_actions(node_id: str, node: dict[str, Any]) -> None:
    actions = node.get("actions")
    if not isinstance(actions, list) or not (1 <= len(actions) <= MAX_ACTIONS_PER_NODE):
        raise FlowDefinitionError(
            f"El nodo '{node_id}' (actions) necesita entre 1 y {MAX_ACTIONS_PER_NODE} 'actions'."
        )
    for index, action in enumerate(actions):
        if not isinstance(action, dict) or action.get("type") not in ACTION_TYPES:
            raise FlowDefinitionError(
                f"La accion {index} de '{node_id}' necesita type en: {', '.join(ACTION_TYPES)}."
            )
        action_type = action["type"]
        is_last = index == len(actions) - 1

        if action_type in ("add_tags", "remove_tags"):
            tags = action.get("tags")
            if not (isinstance(tags, list) and tags and all(isinstance(t, str) and t for t in tags)):
                raise FlowDefinitionError(f"La accion {index} de '{node_id}' necesita 'tags' no vacios.")
        if action_type == "set_fields":
            fields = action.get("fields")
            if not (isinstance(fields, dict) and fields):
                raise FlowDefinitionError(f"La accion {index} de '{node_id}' necesita 'fields'.")
        if action_type == "clear_fields":
            fields = action.get("fields")
            if not (isinstance(fields, list) and fields):
                raise FlowDefinitionError(
                    f"La accion {index} de '{node_id}' necesita 'fields' (lista de campos a borrar)."
                )
        if action_type == "http_request":
            if str(action.get("method", "GET")).upper() not in ("GET", "POST"):
                raise FlowDefinitionError(f"La accion {index} de '{node_id}': method GET o POST.")
            url = str(action.get("url", ""))
            if not url.startswith(("http://", "https://")):
                raise FlowDefinitionError(f"La accion {index} de '{node_id}' necesita 'url' http(s).")
            save = action.get("save")
            if save is not None and not (
                isinstance(save, dict) and all(isinstance(v, str) for v in save.values())
            ):
                raise FlowDefinitionError(
                    f"El 'save' de la accion {index} de '{node_id}' es {{campo: ruta.de.respuesta}}."
                )
        if action_type == "notify_app":
            if not action.get("message"):
                raise FlowDefinitionError(
                    f"La accion {index} de '{node_id}' (notify_app) necesita 'message'."
                )
            emails = action.get("emails")
            if emails is not None and not (
                isinstance(emails, list)
                and emails
                and all(isinstance(e, str) and "@" in e for e in emails)
            ):
                raise FlowDefinitionError(
                    f"Los 'emails' de la accion {index} de '{node_id}' deben ser correos validos."
                )
        if action_type == "start_flow":
            if not action.get("flow"):
                raise FlowDefinitionError(f"La accion {index} de '{node_id}' (start_flow) necesita 'flow'.")
            if not is_last:
                raise FlowDefinitionError(
                    f"start_flow debe ser la ULTIMA accion de '{node_id}': salta a otro flujo."
                )


def _validate_blocks(node_id: str, node: dict[str, Any], nodes: dict[str, Any]) -> None:
    blocks = node.get("blocks")
    if not isinstance(blocks, list) or not (1 <= len(blocks) <= MAX_BLOCKS_PER_NODE):
        raise FlowDefinitionError(
            f"El nodo '{node_id}' (blocks) necesita entre 1 y {MAX_BLOCKS_PER_NODE} 'blocks'."
        )

    seen_option_ids: set[str] = set()
    has_buttons = False
    for index, block in enumerate(blocks):
        if not isinstance(block, dict) or block.get("type") not in BLOCK_TYPES:
            raise FlowDefinitionError(
                f"El bloque {index} de '{node_id}' necesita type en: {', '.join(BLOCK_TYPES)}."
            )
        block_type = block["type"]
        is_last = index == len(blocks) - 1

        if block_type in ("text", "cta", "list", "capture") and not block.get("text"):
            raise FlowDefinitionError(f"El bloque {index} ({block_type}) de '{node_id}' necesita 'text'.")
        if block_type == "cta" and not block.get("url"):
            raise FlowDefinitionError(f"El bloque {index} (cta) de '{node_id}' necesita 'url'.")
        if block_type in ("image", "video", "audio", "document") and not block.get("url"):
            raise FlowDefinitionError(f"El bloque {index} ({block_type}) de '{node_id}' necesita 'url'.")

        if block_type == "wait":
            seconds = block.get("seconds")
            if not isinstance(seconds, int) or not (1 <= seconds <= MAX_BLOCK_WAIT_SECONDS):
                raise FlowDefinitionError(
                    f"El bloque {index} (wait) de '{node_id}' necesita 'seconds' entre 1 y "
                    f"{MAX_BLOCK_WAIT_SECONDS} (para pausas largas esta el nodo delay)."
                )

        if block_type == "text":
            buttons = block.get("buttons") or []
            if not isinstance(buttons, list) or len(buttons) > 3:
                raise FlowDefinitionError(
                    f"El bloque {index} de '{node_id}' admite maximo 3 'buttons' (regla de Meta)."
                )
            has_buttons = has_buttons or bool(buttons)
            for button in buttons:
                if not (isinstance(button, dict) and button.get("id") and button.get("title")):
                    raise FlowDefinitionError(f"Un boton del bloque {index} de '{node_id}' necesita 'id' y 'title'.")
                if button["id"] in seen_option_ids:
                    raise FlowDefinitionError(
                        f"El id de opcion '{button['id']}' se repite dentro de '{node_id}'."
                    )
                seen_option_ids.add(button["id"])
                if button.get("next") is not None and button["next"] not in nodes:
                    raise FlowDefinitionError(
                        f"El boton '{button['id']}' de '{node_id}' apunta a nodo inexistente."
                    )

        if block_type == "list":
            if not is_last:
                raise FlowDefinitionError(
                    f"El bloque list de '{node_id}' debe ser el ULTIMO: la lista espera la eleccion."
                )
            rows = block.get("rows")
            if not isinstance(rows, list) or not (1 <= len(rows) <= MAX_LIST_ROWS):
                raise FlowDefinitionError(
                    f"El bloque list de '{node_id}' necesita entre 1 y {MAX_LIST_ROWS} 'rows'."
                )
            for row in rows:
                if not (isinstance(row, dict) and row.get("id") and row.get("title")):
                    raise FlowDefinitionError(f"Una fila de la lista de '{node_id}' necesita 'id' y 'title'.")
                if row["id"] in seen_option_ids:
                    raise FlowDefinitionError(
                        f"El id de opcion '{row['id']}' se repite dentro de '{node_id}'."
                    )
                seen_option_ids.add(row["id"])
                if row.get("next") is not None and row["next"] not in nodes:
                    raise FlowDefinitionError(f"La fila '{row['id']}' de '{node_id}' apunta a nodo inexistente.")

        if block_type == "capture":
            if not is_last:
                raise FlowDefinitionError(
                    f"El bloque capture de '{node_id}' debe ser el ULTIMO: espera la respuesta."
                )
            if not block.get("field"):
                raise FlowDefinitionError(f"El bloque capture de '{node_id}' necesita 'field'.")
            if has_buttons:
                raise FlowDefinitionError(
                    f"'{node_id}' mezcla botones con captura de texto: la respuesta seria ambigua. "
                    "Separa la captura en su propio paso."
                )


def _node_choice_options(node: dict[str, Any]) -> list[dict[str, Any]]:
    """Las opciones que un nodo en espera acepta como respuesta: botones y
    filas de lista, tanto del nodo plano como de todos sus bloques."""
    options = [*node.get("buttons", []), *node.get("rows", [])]
    for block in node.get("blocks", []):
        options.extend(block.get("buttons", []))
        options.extend(block.get("rows", []))
    return options


def _node_capture_field(node: dict[str, Any]) -> str | None:
    """El custom field si este nodo espera TEXTO libre (capture plano o
    bloque capture al final de un nodo blocks)."""
    if node.get("type") == "capture":
        return str(node.get("field"))
    blocks = node.get("blocks") or []
    if node.get("type") == "blocks" and blocks and blocks[-1].get("type") == "capture":
        return str(blocks[-1].get("field"))
    return None


def _node_waits(node: dict[str, Any]) -> bool:
    if node.get("type") in WAITING_NODE_TYPES:
        return True
    if node.get("type") != "blocks":
        return False
    return _node_capture_field(node) is not None or bool(_node_choice_options(node))


def _validate_when(node_id: str, when: Any) -> None:
    if not isinstance(when, dict) or not when:
        raise FlowDefinitionError(f"El nodo '{node_id}' (condition) necesita 'when'.")
    has_tag = "tag" in when or "not_tag" in when
    ops = [op for op in _CONDITION_OPS if op in when]
    if has_tag:
        if len(when) != 1:
            raise FlowDefinitionError(
                f"El 'when' de '{node_id}' con tag/not_tag no admite otras claves."
            )
    else:
        if not when.get("field") or len(ops) != 1:
            raise FlowDefinitionError(
                f"El 'when' de '{node_id}' necesita 'tag'/'not_tag', o 'field' con exactamente "
                f"una de: {', '.join(_CONDITION_OPS)}."
            )


def _validate_condition(node_id: str, node: dict[str, Any], nodes: dict[str, Any]) -> None:
    cases = node.get("cases")

    if cases is not None:
        # Forma multi-rama (else-if de ManyChat): se evaluan en orden y gana
        # el primer caso que matchee; si ninguno, sigue por 'else'.
        if not isinstance(cases, list) or not (1 <= len(cases) <= MAX_CONDITION_CASES):
            raise FlowDefinitionError(
                f"Los 'cases' de '{node_id}' van de 1 a {MAX_CONDITION_CASES}."
            )
        for index, case in enumerate(cases):
            if not isinstance(case, dict):
                raise FlowDefinitionError(f"El caso {index} de '{node_id}' no es un objeto.")
            _validate_when(node_id, case.get("when"))
            if case.get("next") is not None and case["next"] not in nodes:
                raise FlowDefinitionError(
                    f"El caso {index} de '{node_id}' apunta a nodo inexistente: {case['next']!r}."
                )
        if node.get("else") is not None and node["else"] not in nodes:
            raise FlowDefinitionError(
                f"La rama 'else' de '{node_id}' apunta a nodo inexistente: {node['else']!r}."
            )
        return

    # Forma clasica de una sola condicion: when + then/else.
    _validate_when(node_id, node.get("when"))
    if not node.get("then") and not node.get("else"):
        raise FlowDefinitionError(
            f"El nodo '{node_id}' (condition) necesita al menos una rama 'then' o 'else'."
        )
    for branch in ("then", "else"):
        if node.get(branch) is not None and node[branch] not in nodes:
            raise FlowDefinitionError(
                f"La rama '{branch}' de '{node_id}' apunta a nodo inexistente: {node[branch]!r}."
            )


_PLACEHOLDER = re.compile(r"\{\{\s*([a-zA-Z0-9_.]+)\s*\}\}")


def resolve_context_value(path: str, context: dict[str, Any]) -> str:
    """Ruta con puntos contra el contexto (`fecha`, `contact.name`...).
    Lo inexistente resuelve a cadena vacia."""
    value: Any = context
    for part in path.split("."):
        if isinstance(value, dict) and part in value:
            value = value[part]
        else:
            return ""
    return "" if value is None else str(value)


def interpolate(text: str, context: dict[str, Any]) -> str:
    """`{{fecha}}`, `{{contact.name}}`... contra el contexto. Lo que no
    exista se reemplaza por cadena vacia - un mensaje con un hueco es mejor
    que un `{{fecha}}` literal delante del cliente."""
    return _PLACEHOLDER.sub(lambda m: resolve_context_value(m.group(1), context), text)


def _resolve_condition(node: dict[str, Any], context: dict[str, Any]) -> str | None:
    """A donde sigue un nodo condition: con `cases` (multi-rama) gana el
    primer caso que matchee y si ninguno, `else`; con la forma clasica,
    `then` o `else` segun `when`."""
    cases = node.get("cases")
    if cases is not None:
        for case in cases:
            if _evaluate_when(case.get("when") or {}, context):
                return case.get("next")
        return node.get("else")
    if _evaluate_when(node.get("when") or {}, context):
        return node.get("then")
    return node.get("else")


def _evaluate_condition(node: dict[str, Any], context: dict[str, Any]) -> bool:
    """Compat de tests/llamadas viejas: evalua el `when` clasico del nodo."""
    return _evaluate_when(node.get("when") or {}, context)


def _evaluate_when(when: dict[str, Any], context: dict[str, Any]) -> bool:
    """UN `when` contra el mismo contexto que la interpolacion.
    Comparaciones de texto: sin mayusculas ni espacios en los bordes -
    'Cali ' y 'cali' son la misma respuesta de un humano."""
    tags = [str(t) for t in (context.get("contact") or {}).get("tags") or []]

    if "tag" in when:
        return str(when["tag"]) in tags
    if "not_tag" in when:
        return str(when["not_tag"]) not in tags

    path = str(when.get("field", ""))
    value = resolve_context_value(path, context)
    if value == "" and "." not in path:
        # Nombre simple que no esta en el contexto: es un custom field.
        value = resolve_context_value(f"contact.fields.{path}", context)

    def norm(raw: Any) -> str:
        return str(raw).strip().lower()

    if "equals" in when:
        return norm(value) == norm(when["equals"])
    if "not_equals" in when:
        return norm(value) != norm(when["not_equals"])
    if "contains" in when:
        return norm(when["contains"]) in norm(value)
    if "exists" in when:
        return (value != "") is bool(when["exists"])
    return False


class ContactRepository:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def get_or_create(
        self, app_id: str, business_id: str, phone: str, name: str = ""
    ) -> Contact:
        contact = (
            await self._session.execute(
                select(Contact).where(
                    Contact.app_id == app_id,
                    Contact.business_id == business_id,
                    Contact.phone == phone,
                )
            )
        ).scalar_one_or_none()
        if contact is None:
            contact = Contact(app_id=app_id, business_id=business_id, phone=phone, name=name)
            self._session.add(contact)
        elif name and not contact.name:
            contact.name = name
        return contact

    async def list_contacts(self, app_id: str | None = None) -> list[Contact]:
        query = select(Contact).order_by(Contact.updated_at.desc())
        if app_id:
            query = query.where(Contact.app_id == app_id)
        return list((await self._session.execute(query)).scalars())


def _apply_node_effects(contact: Contact, node: dict[str, Any], context: dict[str, Any]) -> None:
    """Tags y fields: el intercambio de datos entre flujos y apps. Se
    reasignan listas/dicts completos (no mutacion in-place) para que
    SQLAlchemy detecte el cambio en columnas JSON."""
    add = [str(t) for t in node.get("add_tags", [])]
    remove = {str(t) for t in node.get("remove_tags", [])}
    if add or remove:
        contact.tags = [t for t in dict.fromkeys([*contact.tags, *add]) if t not in remove]
    sets = node.get("set_fields")
    if isinstance(sets, dict) and sets:
        contact.fields = {
            **contact.fields,
            **{str(k): interpolate(str(v), context) for k, v in sets.items()},
        }


class FlowRunner:
    """Ejecuta nodos de UN flujo para UN contacto, enviando por la misma
    identidad de WhatsApp que usaria un envio normal de ese negocio."""

    def __init__(self, session: AsyncSession, app: AppIdentity, flow: Flow, contact: Contact) -> None:
        self._session = session
        self._app = app
        self._flow = flow
        self._contact = contact

    async def run_from(self, flow_session: FlowSession, node_id: str | None) -> None:
        nodes = self._flow.definition.get("nodes", {})
        steps = 0

        while node_id is not None and steps < MAX_NODES_PER_RUN:
            node = nodes.get(node_id)
            if node is None:
                logger.warning(
                    "flows.node_missing", extra={"flow_id": self._flow.id, "node_id": node_id}
                )
                break
            steps += 1

            context = {**flow_session.context, "contact": _contact_context(self._contact)}
            _apply_node_effects(self._contact, node, context)

            if node["type"] == "condition":
                # No envia nada: solo decide la rama. El contexto se rearma
                # con los tags/fields que este nodo acabara de aplicar.
                context = {**flow_session.context, "contact": _contact_context(self._contact)}
                node_id = _resolve_condition(node, context)
                continue

            if node["type"] == "actions":
                jumped = await self._run_actions(node, flow_session, context)
                if jumped:
                    # start_flow arranco OTRO flujo: esa llamada ya
                    # supersedio esta sesion - nada mas que escribir aqui.
                    return
                node_id = node.get("next")
                continue

            if node["type"] == "random":
                # Aleatorizador (A/B de ManyChat): tira el dado ponderado y
                # sigue. Se re-tira en cada corrida a proposito - un split
                # pegajoso por contacto seria un tag, no un dado.
                node_id = _pick_random_branch(node)
                continue

            if node["type"] == "delay":
                next_id = node.get("next")
                if next_id is None:
                    # Un delay sin continuacion no espera nada.
                    break
                flow_session.current_node = next_id
                flow_session.status = "waiting"
                flow_session.resume_at = datetime.utcnow() + timedelta(minutes=int(node["minutes"]))
                flow_session.updated_at = datetime.utcnow()
                await self._session.commit()
                return

            if node["type"] == "blocks":
                await self._send_blocks(node, context)
            else:
                await self._send_node(node, context)

            if _node_waits(node):
                # Parada: la sesion queda esperando la respuesta aqui
                # (boton, opcion de la lista, o el texto libre de capture).
                flow_session.current_node = node_id
                flow_session.status = "active"
                flow_session.resume_at = None
                flow_session.updated_at = datetime.utcnow()
                await self._session.commit()
                return

            node_id = node.get("next")

        if steps >= MAX_NODES_PER_RUN:
            logger.warning("flows.max_nodes_reached", extra={"flow_id": self._flow.id})

        flow_session.current_node = None
        flow_session.status = "completed"
        flow_session.resume_at = None
        flow_session.updated_at = datetime.utcnow()
        await self._session.commit()

    async def _send_node(self, node: dict[str, Any], context: dict[str, Any]) -> None:
        node_type = node["type"]
        text = interpolate(str(node.get("text", "")), context)
        template_params = [interpolate(str(p), context) for p in node.get("params", [])]
        message = OutboundMessage(
            to=self._contact.phone,
            text=text,
            # Respuestas dentro de la conversacion: categoria service (Meta
            # no la cobra dentro de la ventana de 24h). La excepcion es el
            # nodo template: existe justo para REABRIR la conversacion (p.ej.
            # despues de un delay largo) y se cobra como utility.
            category="utility" if node_type == "template" else "service",
            template_name=str(node["template"]) if node_type == "template" else None,
            template_language=str(node.get("language", "es")) if node_type == "template" else None,
            template_components=[
                {
                    "type": "body",
                    "parameters": [{"type": "text", "text": p} for p in template_params],
                }
            ]
            if node_type == "template" and template_params
            else [],
            buttons=[
                {"id": str(b["id"]), "title": interpolate(str(b["title"]), context)}
                for b in node.get("buttons", [])
            ]
            if node_type == "buttons"
            else [],
            cta_url=interpolate(str(node["url"]), context) if node_type == "cta_url" else None,
            cta_title=str(node.get("button", "Abrir")) if node_type == "cta_url" else None,
            media_kind=str(node["kind"]) if node_type == "media" else None,
            media_url=interpolate(str(node["url"]), context) if node_type == "media" else None,
            media_caption=interpolate(str(node.get("caption", "")), context) or None
            if node_type == "media"
            else None,
            media_filename=str(node.get("filename", "")) or None if node_type == "media" else None,
            list_button=str(node.get("button", "")) or None if node_type == "list" else None,
            list_rows=[
                {
                    "id": str(r["id"]),
                    "title": interpolate(str(r["title"]), context),
                    **(
                        {"description": interpolate(str(r["description"]), context)}
                        if r.get("description")
                        else {}
                    ),
                }
                for r in node.get("rows", [])
            ]
            if node_type == "list"
            else [],
            # Producto(s) del catalogo: un retailer_id = SPM; sections = MPM.
            # El catalog_id lo pone el canal desde la identidad efectiva.
            product_retailer_id=(
                str(node["retailer_id"]) if node_type == "product" and node.get("retailer_id") else None
            ),
            product_sections=[
                {
                    "title": str(s["title"]),
                    "product_retailer_ids": [str(r) for r in s["retailer_ids"]],
                }
                for s in node.get("sections", [])
            ]
            if node_type == "product" and node.get("sections")
            else [],
            product_header=str(node.get("header", "")) or None if node_type == "product" else None,
        )
        await self._deliver(message)

    async def _send_blocks(self, node: dict[str, Any], context: dict[str, Any]) -> None:
        """El paso 'Enviar mensaje' de ManyChat: cada bloque de contenido es
        SU PROPIO mensaje de WhatsApp, enviados seguidos; `wait` es la pausa
        corta entre textos (el efecto 'esta escribiendo')."""
        for block in node.get("blocks", []):
            block_type = block.get("type")
            text = interpolate(str(block.get("text", "")), context)

            if block_type == "wait":
                await asyncio.sleep(min(int(block.get("seconds", 1)), MAX_BLOCK_WAIT_SECONDS))
                continue

            message = OutboundMessage(
                to=self._contact.phone,
                text=text if block_type not in MEDIA_KINDS else None,
                category="service",
                buttons=[
                    {"id": str(b["id"]), "title": interpolate(str(b["title"]), context)}
                    for b in block.get("buttons", [])
                ]
                if block_type == "text"
                else [],
                cta_url=interpolate(str(block["url"]), context) if block_type == "cta" else None,
                cta_title=str(block.get("button", "Abrir")) if block_type == "cta" else None,
                media_kind=block_type if block_type in MEDIA_KINDS else None,
                media_url=interpolate(str(block.get("url", "")), context)
                if block_type in MEDIA_KINDS
                else None,
                media_caption=interpolate(str(block.get("caption", "")), context) or None
                if block_type in MEDIA_KINDS
                else None,
                media_filename=str(block.get("filename", "")) or None
                if block_type == "document"
                else None,
                list_button=str(block.get("button", "")) or None if block_type == "list" else None,
                list_rows=[
                    {
                        "id": str(r["id"]),
                        "title": interpolate(str(r["title"]), context),
                        **(
                            {"description": interpolate(str(r["description"]), context)}
                            if r.get("description")
                            else {}
                        ),
                    }
                    for r in block.get("rows", [])
                ]
                if block_type == "list"
                else [],
            )
            await self._deliver(message)

    async def _run_actions(
        self, node: dict[str, Any], flow_session: FlowSession, context: dict[str, Any]
    ) -> bool:
        """Ejecuta las acciones del nodo en orden. @return True si la ultima
        fue start_flow (esta sesion quedo superseded por el flujo nuevo).
        Todo es fail-soft: una API caida deja log, nunca rompe el flujo."""
        for action in node.get("actions", []):
            action_type = action.get("type")
            try:
                if action_type == "add_tags":
                    self._contact.tags = list(
                        dict.fromkeys([*self._contact.tags, *[str(t) for t in action["tags"]]])
                    )
                elif action_type == "remove_tags":
                    gone = {str(t) for t in action["tags"]}
                    self._contact.tags = [t for t in self._contact.tags if t not in gone]
                elif action_type == "set_fields":
                    self._contact.fields = {
                        **self._contact.fields,
                        **{str(k): interpolate(str(v), context) for k, v in action["fields"].items()},
                    }
                elif action_type == "clear_fields":
                    gone = {str(k) for k in action["fields"]}
                    self._contact.fields = {
                        k: v for k, v in self._contact.fields.items() if k not in gone
                    }
                elif action_type == "http_request":
                    await self._do_http_request(action, context)
                elif action_type == "notify_app":
                    await self._do_notify_app(action, context)
                elif action_type == "start_flow" and await self._do_start_flow(
                    action, flow_session
                ):
                    return True
                # El contexto se rearma con lo que cada accion cambio.
                context = {**flow_session.context, "contact": _contact_context(self._contact)}
            except Exception:
                logger.exception(
                    "flows.action_failed",
                    extra={"flow_id": self._flow.id, "action": action_type},
                )
        return False

    async def _do_http_request(self, action: dict[str, Any], context: dict[str, Any]) -> None:
        """La 'Solicitud externa' de ManyChat: pega a la API del negocio con
        el contexto interpolado y (opcional) guarda partes de la respuesta
        en los custom fields del contacto."""
        url = interpolate(str(action["url"]), context)
        if not _is_safe_url(url):
            logger.warning("flows.http_action_blocked", extra={"url": url[:120]})
            return
        method = str(action.get("method", "GET")).upper()
        headers = {
            str(k): interpolate(str(v), context) for k, v in (action.get("headers") or {}).items()
        }
        body = _interpolate_deep(action.get("body"), context)

        async with httpx.AsyncClient(timeout=HTTP_ACTION_TIMEOUT_SECONDS) as client:
            if method == "POST":
                response = await client.post(url, json=body, headers=headers)
            else:
                response = await client.get(url, headers=headers)

        if response.status_code >= 400:
            logger.warning(
                "flows.http_action_error",
                extra={"url": url[:120], "status": response.status_code},
            )
            return

        save = action.get("save") or {}
        if save:
            try:
                data = response.json()
            except ValueError:
                logger.warning("flows.http_action_not_json", extra={"url": url[:120]})
                return
            updates: dict[str, str] = {}
            for campo, ruta in save.items():
                value: Any = data
                for part in str(ruta).split("."):
                    if isinstance(value, dict) and part in value:
                        value = value[part]
                    elif isinstance(value, list) and part.isdigit() and int(part) < len(value):
                        value = value[int(part)]
                    else:
                        value = None
                        break
                if value is not None:
                    updates[str(campo)] = value if isinstance(value, str) else json.dumps(value, ensure_ascii=False)
            if updates:
                self._contact.fields = {**self._contact.fields, **updates}

    async def _do_notify_app(self, action: dict[str, Any], context: dict[str, Any]) -> None:
        """El 'notificar a la bandeja': un evento firmado al callback de la
        app duena (la bandeja de Connect ES la app - principio 45), y
        ademas correo directo a admins/agentes si la accion trae 'emails'
        (el caso "pidio hablar con un humano" no puede depender de que
        alguien este mirando un panel)."""
        message_text = interpolate(str(action["message"]), context)

        whatsapp = self._app.whatsapp
        if whatsapp is not None and whatsapp.callback_url and whatsapp.callback_secret:
            payload = json.dumps(
                {
                    "object": "nexolu-comms",
                    "event": "flow_notify",
                    "flow": self._flow.name,
                    "message": message_text,
                    "business_id": self._contact.business_id,
                    "contact": _contact_context(self._contact),
                },
                ensure_ascii=False,
            ).encode()
            headers = {
                "Content-Type": "application/json",
                "X-Nexolu-Event": "flow-notify",
                **build_forward_headers(payload, whatsapp.callback_secret),
            }
            async with httpx.AsyncClient(timeout=HTTP_ACTION_TIMEOUT_SECONDS) as client:
                await client.post(whatsapp.callback_url, content=payload, headers=headers)
        else:
            logger.info("flows.notify_skipped_no_callback", extra={"app_id": self._app.app_id})

        emails = [str(e) for e in action.get("emails") or []]
        if emails:
            email_sender = get_channel_registry().resolve("email")
            body = (
                f"{message_text}\n\n"
                f"Contacto: {self._contact.name or '(sin nombre)'} · {self._contact.phone}\n"
                f"Flujo: {self._flow.name}\n"
                f"Responde desde la bandeja: https://connect.nexolu.co/chat"
            )
            for email in emails:
                result = await email_sender.send(
                    self._app,
                    OutboundMessage(
                        to=email,
                        subject=f"[Connect] {message_text[:80]}",
                        text=body,
                    ),
                )
                NotificationRepository(self._session).log(
                    app_id=self._app.app_id,
                    business_id=self._contact.business_id or self._app.app_id,
                    channel="email",
                    recipient=email,
                    status=result.status,
                    reference=f"flow:{self._flow.name}",
                    provider_message_id=result.provider_message_id,
                    error=result.error,
                    cost_micros=result.cost_micros,
                )

    async def _do_start_flow(self, action: dict[str, Any], flow_session: FlowSession) -> bool:
        """El goto de ManyChat: salta a otro flujo de la misma app (hereda
        las variables del contexto). start_flow() supersede esta sesion."""
        target = (
            await self._session.execute(
                select(Flow).where(
                    Flow.app_id == self._app.app_id,
                    Flow.business_id.in_(["", self._contact.business_id]),
                    Flow.name == str(action["flow"]),
                    Flow.is_active.is_(True),
                )
            )
        ).scalars().first()
        if target is None or target.id == self._flow.id:
            logger.warning(
                "flows.start_flow_missing",
                extra={"flow_id": self._flow.id, "target": str(action["flow"])[:64]},
            )
            return False
        await start_flow(
            self._session,
            self._app,
            target,
            phone=self._contact.phone,
            business_id=self._contact.business_id or None,
            variables=dict(flow_session.context),
            contact_name=self._contact.name,
        )
        return True

    async def _deliver(self, message: OutboundMessage) -> None:
        identity, _ = await resolve_whatsapp_identity(
            self._session, self._app, self._contact.business_id or self._app.app_id
        )
        sender = get_channel_registry().resolve("whatsapp")
        result = await sender.send(identity, message)

        # Auditoria/costos: cada mensaje del motor es una Notification mas,
        # rastreable por reference=flow:<id>.
        NotificationRepository(self._session).log(
            app_id=self._app.app_id,
            business_id=self._contact.business_id or self._app.app_id,
            channel="whatsapp",
            recipient=self._contact.phone,
            status=result.status,
            reference=f"flow:{self._flow.name}",
            provider_message_id=result.provider_message_id,
            error=result.error,
            cost_micros=result.cost_micros,
        )
        if result.status != "sent":
            logger.warning(
                "flows.send_failed",
                extra={"flow_id": self._flow.id, "contact_id": self._contact.id, "error": result.error},
            )

        # La bandeja tambien registra lo que el motor envia: la conversacion
        # completa (humano + bot) se lee en un solo hilo.
        self._session.add(
            ChatMessage(
                app_id=self._app.app_id,
                business_id=self._contact.business_id,
                contact_id=self._contact.id,
                direction="out",
                message_type=_outbound_chat_type(message),
                body=message.text or message.media_caption or "",
                payload=_outbound_chat_payload(message),
                wamid=result.provider_message_id,
                status=result.status,
                origin="flow",
            )
        )


def _is_safe_url(url: str) -> bool:
    """Guardas minimas contra SSRF para la solicitud externa: solo http(s)
    y nunca hacia loopback/privadas - el panel es de confianza, la red
    interna del droplet no tiene por que estar expuesta a un typo."""
    try:
        parsed = urlparse(url)
    except ValueError:
        return False
    if parsed.scheme not in ("http", "https") or not parsed.hostname:
        return False
    host = parsed.hostname
    if host.lower() in ("localhost",):
        return False
    try:
        ip = ipaddress.ip_address(host)
    except ValueError:
        return True  # hostname DNS: permitido
    return not (ip.is_private or ip.is_loopback or ip.is_link_local or ip.is_reserved or ip.is_unspecified)


def _interpolate_deep(value: Any, context: dict[str, Any]) -> Any:
    """Interpola {{variables}} dentro de un body JSON anidado."""
    if isinstance(value, str):
        return interpolate(value, context)
    if isinstance(value, dict):
        return {k: _interpolate_deep(v, context) for k, v in value.items()}
    if isinstance(value, list):
        return [_interpolate_deep(v, context) for v in value]
    return value


def _pick_random_branch(node: dict[str, Any]) -> str | None:
    """El dado ponderado del aleatorizador. @return el `next` de la rama
    elegida (None = esa rama termina el flujo)."""
    branches = [b for b in node.get("branches", []) if isinstance(b, dict)]
    if not branches:
        return None
    weights = [max(1, int(b.get("weight", 1))) for b in branches]
    chosen = random.choices(branches, weights=weights, k=1)[0]
    target = chosen.get("next")
    return str(target) if target else None


def _contact_context(contact: Contact) -> dict[str, Any]:
    return {"name": contact.name, "phone": contact.phone, "tags": contact.tags, "fields": contact.fields}


async def start_flow(
    session: AsyncSession,
    app: AppIdentity,
    flow: Flow,
    *,
    phone: str,
    business_id: str | None = None,
    variables: dict[str, Any] | None = None,
    contact_name: str = "",
) -> FlowSession:
    contact = await ContactRepository(session).get_or_create(
        app.app_id, business_id or flow.business_id or "", phone, name=contact_name
    )
    await session.flush()

    # El flujo mas reciente gana: una sola sesion viva por contacto
    # (activa esperando botones o waiting en un delay).
    stale = (
        await session.execute(
            select(FlowSession).where(
                FlowSession.contact_id == contact.id,
                FlowSession.status.in_(["active", "waiting"]),
            )
        )
    ).scalars()
    for old in stale:
        old.status = "superseded"

    flow_session = FlowSession(
        flow_id=flow.id,
        contact_id=contact.id,
        app_id=app.app_id,
        business_id=contact.business_id,
        context=dict(variables or {}),
    )
    session.add(flow_session)
    await session.flush()

    await FlowRunner(session, app, flow, contact).run_from(
        flow_session, flow.definition.get("start")
    )
    return flow_session


async def handle_inbound_event(event_id: str) -> None:
    """Punto de entrada desde el webhook (background task). Nunca lanza."""
    try:
        await _handle_inbound_event(event_id)
    except Exception:
        logger.exception("flows.inbound_error", extra={"event_id": event_id})


async def _handle_inbound_event(event_id: str) -> None:
    async with get_sessionmaker()() as session:
        event = await session.get(WebhookEvent, event_id)
        if event is None or event.event_type != "message":
            return

        inbound = _parse_inbound(event.payload)
        if inbound is None:
            return
        phone, text, button_id, profile_name = inbound

        app = await resolve_by_app_id(session, event.app_id)
        if app is None or app.whatsapp is None:
            return

        business_id = ""
        if event.business_channel_id:
            from nexolu_comms_api.core.db.entities import BusinessChannel

            channel = await session.get(BusinessChannel, event.business_channel_id)
            if channel is not None:
                business_id = channel.business_id

        contact = await ContactRepository(session).get_or_create(
            app.app_id, business_id, phone, name=profile_name
        )
        contact.last_inbound_at = datetime.utcnow()
        await session.flush()

        # La bandeja: TODO mensaje entrante queda en el historial del chat,
        # responda el motor o no (la conversacion humana vive aqui).
        _log_inbound_chat(session, app.app_id, business_id, contact.id, event.payload, text)

        # 1) ¿Hay una sesion esperando en botones? La respuesta avanza el flujo.
        active = (
            await session.execute(
                select(FlowSession).where(
                    FlowSession.contact_id == contact.id, FlowSession.status == "active"
                )
            )
        ).scalars().first()

        if active is not None and active.updated_at < datetime.utcnow() - timedelta(
            hours=SESSION_TTL_HOURS
        ):
            active.status = "expired"
            await session.commit()
            active = None

        if active is not None and active.current_node:
            flow = await session.get(Flow, active.flow_id)
            if flow is None or not flow.is_active:
                active.status = "expired"
                await session.commit()
                return
            node = flow.definition.get("nodes", {}).get(active.current_node, {})

            capture_field = _node_capture_field(node)
            if capture_field:
                # Recopilacion de datos: CUALQUIER texto es la respuesta y
                # queda en el custom field del contacto. Un mensaje sin
                # texto (sticker, audio) no cuenta: se sigue esperando.
                if not text:
                    await session.commit()
                    return
                contact.fields = {**contact.fields, capture_field: text.strip()}
                blocks = node.get("blocks") or []
                next_id = (
                    (blocks[-1].get("next") if blocks else None) or node.get("next")
                    if node.get("type") == "blocks"
                    else node.get("next")
                )
                await FlowRunner(session, app, flow, contact).run_from(active, next_id)
                return

            chosen = _match_choice(node, button_id, text)
            if chosen is None:
                # Respondio otra cosa: la conversacion es de la app duena,
                # el motor no insiste. La sesion sigue esperando.
                await session.commit()
                return
            await FlowRunner(session, app, flow, contact).run_from(active, chosen.get("next"))
            return

        # 2) Sin sesion: ¿algun keyword de un flujo activo matchea?
        if not text:
            await session.commit()
            return
        normalized = text.strip().lower()
        flows = (
            await session.execute(
                select(Flow).where(
                    Flow.app_id == app.app_id,
                    Flow.business_id.in_(["", business_id]),
                    Flow.is_active.is_(True),
                    Flow.trigger_type == "keyword",
                )
            )
        ).scalars()
        for flow in flows:
            if normalized in [str(k).strip().lower() for k in flow.trigger_keywords]:
                await start_flow(
                    session, app, flow, phone=phone, business_id=business_id, contact_name=profile_name
                )
                return
        await session.commit()


def _outbound_chat_type(message: OutboundMessage) -> str:
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


def _outbound_chat_payload(message: OutboundMessage) -> dict[str, Any]:
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
    return payload


def _log_inbound_chat(
    session: AsyncSession,
    app_id: str,
    business_id: str,
    contact_id: str,
    raw_payload: str,
    parsed_text: str | None,
) -> None:
    """Guarda el mensaje entrante en el historial del chat (fail-soft: la
    bandeja nunca puede tumbar el procesamiento del webhook)."""
    try:
        raw = json.loads(raw_payload)["entry"][0]["changes"][0]["value"]["messages"][0]
    except (ValueError, KeyError, IndexError, TypeError):
        return
    message_type = str(raw.get("type") or "text")
    body = parsed_text or ""
    if not body and isinstance(raw.get(message_type), dict):
        body = str(raw[message_type].get("caption") or "")
    session.add(
        ChatMessage(
            app_id=app_id,
            business_id=business_id,
            contact_id=contact_id,
            direction="in",
            message_type=message_type,
            body=body,
            payload=raw,
            wamid=str(raw.get("id") or "") or None,
        )
    )


def _match_choice(
    node: dict[str, Any], choice_id: str | None, text: str | None
) -> dict[str, Any] | None:
    """La opcion elegida en un nodo que espera: boton o fila de lista, del
    nodo plano o de sus bloques. Mismo contrato: {id, title, next?}."""
    for option in _node_choice_options(node):
        if choice_id and str(option.get("id")) == choice_id:
            return option
        # Tolerancia: el usuario escribio el titulo en vez de tocar la opcion.
        if text and str(option.get("title", "")).strip().lower() == text.strip().lower():
            return option
    return None


def _parse_inbound(payload: str) -> tuple[str, str | None, str | None, str] | None:
    """@return (phone, text, button_reply_id, profile_name) o None."""
    try:
        value = json.loads(payload)["entry"][0]["changes"][0]["value"]
        message = value["messages"][0]
        phone = str(message["from"])
    except (ValueError, KeyError, IndexError, TypeError):
        return None

    profile_name = ""
    contacts = value.get("contacts")
    if isinstance(contacts, list) and contacts:
        profile_name = str((contacts[0].get("profile") or {}).get("name") or "")

    text: str | None = None
    button_id: str | None = None
    if message.get("type") == "text":
        text = str((message.get("text") or {}).get("body") or "")
    elif message.get("type") == "interactive":
        interactive = message.get("interactive") or {}
        # button_reply (botones) o list_reply (mensaje de lista): mismo
        # contrato {id, title} para el motor.
        reply = interactive.get("button_reply") or interactive.get("list_reply") or {}
        button_id = str(reply.get("id")) if reply.get("id") else None
        text = str(reply.get("title")) if reply.get("title") else None
    elif message.get("type") == "button":
        # Boton de plantilla (quick reply de template).
        reply = message.get("button") or {}
        text = str(reply.get("text")) if reply.get("text") else None
        button_id = str(reply.get("payload")) if reply.get("payload") else None

    return phone, text, button_id, profile_name


async def resume_due_sessions() -> int:
    """Retoma las sesiones `waiting` con el delay vencido. Cada sesion se
    procesa aislada: una que falle no bloquea a las demas.
    @return cuantas se intentaron."""
    now = datetime.utcnow()
    async with get_sessionmaker()() as session:
        result = await session.execute(
            select(FlowSession.id)
            .where(FlowSession.status == "waiting", FlowSession.resume_at <= now)
            .order_by(FlowSession.resume_at)
            .limit(50)
        )
        due = [row[0] for row in result]

    for session_id in due:
        try:
            await _resume_session(session_id)
        except Exception:
            logger.exception("flows.resume_error", extra={"session_id": session_id})
            # Marcarla expirada evita reintentarla en caliente cada tick;
            # el detalle ya quedo en el log.
            async with get_sessionmaker()() as session:
                broken = await session.get(FlowSession, session_id)
                if broken is not None and broken.status == "waiting":
                    broken.status = "expired"
                    await session.commit()

    return len(due)


async def _resume_session(session_id: str) -> None:
    async with get_sessionmaker()() as session:
        flow_session = await session.get(FlowSession, session_id)
        if flow_session is None or flow_session.status != "waiting":
            return

        flow = await session.get(Flow, flow_session.flow_id)
        contact = await session.get(Contact, flow_session.contact_id)
        app = await resolve_by_app_id(session, flow_session.app_id)
        if flow is None or not flow.is_active or contact is None or app is None:
            flow_session.status = "expired"
            flow_session.resume_at = None
            await session.commit()
            return

        node_id = flow_session.current_node
        flow_session.resume_at = None
        await FlowRunner(session, app, flow, contact).run_from(flow_session, node_id)


async def flow_resume_worker_loop() -> None:
    """Task de proceso (arrancada en el lifespan), calcada del worker de
    reintento de webhooks: duerme, retoma lo vencido, y nunca muere en
    silencio."""
    from nexolu_comms_api.config import get_settings

    settings = get_settings()
    while True:
        await asyncio.sleep(settings.flow_resume_interval_seconds)
        try:
            await resume_due_sessions()
        except Exception:
            logger.exception("flows.resume_worker_error")
