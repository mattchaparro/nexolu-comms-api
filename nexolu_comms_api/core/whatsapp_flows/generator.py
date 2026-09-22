"""Generacion de un Flow JSON a partir de una descripcion en lenguaje natural.

"formulario para confirmar cita: servicio y fecha de solo lectura, hora de
una lista, nombre opcional" -> Flow JSON listo para subir.

El modelo lo pone nexolu-ia-core (POST /v1/completions, una sola pasada):
Connect es una app mas del Core, con su propio registro de uso/costo por
negocio, y el proveedor/modelo se elige alla (anthropic / claude-opus-5 -
ver `ia_core_*` en config.py).

Nada de lo generado se muestra sin validar. El bucle:

1. Pedirle el JSON al modelo.
2. Validador local (validator.py). Si falla, se le devuelven los errores.
3. Validador real de Meta: se sube como asset a un BORRADOR y se leen los
   validation_errors (`validate_remote`, lo arma quien llama - el borrador
   se crea recien con el primer JSON que pasa lo local).
4. Si Meta devuelve errores, se reintenta pasandoselos: maximo
   `MAX_RETRIES` reintentos (local y Meta comparten el presupuesto, para
   que el peor caso tenga techo en tiempo y en plata).

Si se agotan los intentos se devuelve el ultimo JSON CON sus errores: el
operador lo corrige a mano en el editor, que es mas rapido que empezar de
cero.
"""
from __future__ import annotations

import json
import logging
import re
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import Any

import httpx

from nexolu_comms_api.config import get_settings
from nexolu_comms_api.core.whatsapp_flows.library import LIBRARY, fresh_json
from nexolu_comms_api.core.whatsapp_flows.validator import (
    COMPONENT_TYPES,
    FlowIssue,
    has_errors,
    validate_flow_json,
)

logger = logging.getLogger(__name__)

MAX_RETRIES = 2
MAX_TOKENS = 8000


class FlowGenerationUnavailable(Exception):
    """ia-core no esta configurado o no respondio."""


@dataclass
class GenerationResult:
    flow_json: dict[str, Any] | None
    issues: list[FlowIssue] = field(default_factory=list)
    attempts: int = 0

    @property
    def ok(self) -> bool:
        return self.flow_json is not None and not has_errors(self.issues)


RemoteValidator = Callable[[dict[str, Any]], Awaitable[list[FlowIssue]]]


SYSTEM_PROMPT = f"""Eres experto en WhatsApp Flows de Meta. Escribes el Flow JSON de un \
formulario nativo de WhatsApp a partir de la descripcion de un negocio.

Responde SOLO con el Flow JSON (un objeto), sin texto antes ni despues y sin bloques de codigo.

Reglas (el JSON se valida contra Meta; si rompes una, se rechaza):
- "version": "7.2". Sin "data_api_version" ni "routing_model": el formulario es estatico, sin \
endpoint. Nunca uses la accion "data_exchange".
- Cada pantalla: "id" en MAYUSCULAS con guion bajo (nunca "SUCCESS"), "title", \
"layout": {{"type": "SingleColumnLayout", "children": [...]}}.
- Componentes validos (respeta mayusculas): {", ".join(COMPONENT_TYPES)}.
- Los campos del usuario van dentro de un "Form" con "name". El valor inicial de un campo va \
en "init-values" DEL FORM ({{"<name del campo>": "..."}}); NUNCA "init-value" en el componente.
- Datos que el negocio carga al enviar (lo de solo lectura, las opciones de una lista \
dinamica): se declaran en "data" de la pantalla, cada uno con "type" y "__example__" \
(obligatorio: llena la vista previa), y se usan como "${{data.<clave>}}".
- Lo que la persona escribe se lee como "${{form.<name>}}" y solo en la misma pantalla.
- La ultima pantalla lleva "terminal": true y un "Footer" con \
"on-click-action": {{"name": "complete", "payload": {{...}}}}: el payload es lo que recibe el \
negocio; incluye cada dato y cada campo. Pantallas intermedias: Footer con \
{{"name": "navigate", "next": {{"type": "screen", "name": "<ID>"}}, "payload": {{...}}}} y lo \
que se pase en payload se declara en "data" de la pantalla destino.
- Un solo Footer por pantalla. Limites: label del Footer <= 35 caracteres; TextHeading <= 80; \
label de TextInput/TextArea <= 20; label de Dropdown/RadioButtonsGroup/CheckboxGroup <= 30; \
"title" de cada opcion <= 30, y cada opcion con "id" y "title".
- "Solo lectura" = mostrarlo como texto (TextHeading/TextBody/TextCaption con ${{data.x}}) y \
mandarlo en el payload desde ${{data.x}}; no es un campo editable.
- Textos visibles en espanol neutro, cortos y amables. Claves (name, ids, payload) en \
minusculas con guion bajo.

Ejemplo aceptado por Meta (confirmar una cita):
{json.dumps(fresh_json(LIBRARY["confirm_booking"]), ensure_ascii=False)}
"""


def _user_prompt(description: str, previous: str | None, issues: list[FlowIssue]) -> str:
    if previous is None:
        return f"Formulario a construir:\n{description}"
    problems = "\n".join(f"- {issue.path or '(general)'}: {issue.message}" for issue in issues)
    return (
        f"Formulario a construir:\n{description}\n\n"
        f"Tu intento anterior:\n{previous}\n\n"
        f"Fue rechazado por estos errores; corrigelos y devuelve el JSON completo:\n{problems}"
    )


def extract_json(text: str) -> dict[str, Any] | None:
    """El objeto JSON de la respuesta, tolerando ```json ... ``` o texto
    alrededor (el prompt lo prohibe, pero no se confia en eso)."""
    fenced = re.search(r"```(?:json)?\s*(\{.*\})\s*```", text, re.DOTALL)
    candidate = fenced.group(1) if fenced else text
    start, end = candidate.find("{"), candidate.rfind("}")
    if start == -1 or end <= start:
        return None
    try:
        parsed = json.loads(candidate[start : end + 1])
    except ValueError:
        return None
    return parsed if isinstance(parsed, dict) else None


async def complete(system: str, user: str, *, business_id: str | None) -> str:
    """Una pasada por nexolu-ia-core. @raise FlowGenerationUnavailable."""
    settings = get_settings()
    if not (settings.ia_core_base_url and settings.ia_core_api_key):
        raise FlowGenerationUnavailable(
            "La generacion con IA no esta configurada (IA_CORE_BASE_URL / IA_CORE_API_KEY)."
        )
    body = {
        "system": system,
        "user": user,
        "max_tokens": MAX_TOKENS,
        "context": {"business_id": business_id, "user_id": "connect-panel", "channel": "web"},
    }
    try:
        async with httpx.AsyncClient(timeout=settings.ia_core_timeout_seconds) as client:
            response = await client.post(
                f"{settings.ia_core_base_url.rstrip('/')}/v1/completions",
                json=body,
                headers={"Authorization": f"Bearer {settings.ia_core_api_key}"},
            )
    except httpx.HTTPError as exc:
        raise FlowGenerationUnavailable(f"No se pudo contactar a IA Core: {exc}") from exc
    if response.is_error:
        logger.warning("flow_generator.ia_core_rejected", extra={"status_code": response.status_code})
        raise FlowGenerationUnavailable(f"IA Core respondio {response.status_code}.")
    return str(response.json().get("text") or "")


async def generate_flow_json(
    description: str,
    *,
    business_id: str | None,
    validate_remote: RemoteValidator | None = None,
) -> GenerationResult:
    result = GenerationResult(flow_json=None)
    previous_text: str | None = None

    for _ in range(1 + MAX_RETRIES):
        result.attempts += 1
        text = await complete(
            SYSTEM_PROMPT, _user_prompt(description, previous_text, result.issues), business_id=business_id
        )
        previous_text = text
        flow_json = extract_json(text)
        if flow_json is None:
            result.issues = [FlowIssue("La respuesta no fue un objeto JSON valido.")]
            continue

        result.flow_json = flow_json
        result.issues = validate_flow_json(flow_json)
        if has_errors(result.issues):
            continue

        if validate_remote is not None:
            warnings = result.issues
            meta = await validate_remote(flow_json)
            result.issues = warnings + meta
            if has_errors(meta):
                previous_text = json.dumps(flow_json, ensure_ascii=False, indent=2)
                continue
        break

    logger.info(
        "flow_generator.done",
        extra={"attempts": result.attempts, "ok": result.ok, "issues": len(result.issues)},
    )
    return result
