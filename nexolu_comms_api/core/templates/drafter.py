"""Redactar una plantilla de WhatsApp con IA a partir de lo que se quiere decir.

"avisar que el lunes festivo abrimos de 9 a 3" -> nombre, categoria, cuerpo
con {{1}} para el nombre, pie y botones, listos para revisar y mandar a Meta.

Lo que se cuida es que Meta la APRUEBE: una plantilla rechazada es un dia
perdido. Por eso las reglas de Meta van en el prompt, y lo que vuelve se
valida aca (validate_draft); si algo no cumple se le devuelve al modelo una
vez para que lo corrija. Nada se crea en Meta desde aca: el borrador vuelve
al panel y la persona decide.

El modelo lo pone nexolu-ia-core, por el mismo camino que la generacion de
Formularios (core/whatsapp_flows/generator.py:complete).
"""
from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from typing import Any

from nexolu_comms_api.core.whatsapp_flows.generator import complete, extract_json

MAX_RETRIES = 1
MAX_BUTTONS = 3
MAX_BUTTON_CHARS = 25
MAX_BODY_CHARS = 1024
MAX_FOOTER_CHARS = 60

SYSTEM_PROMPT = """Redactas plantillas de mensajes de WhatsApp Business para que Meta las APRUEBE.
Escribes en español de Colombia, cálido y corto, como escribe un salón de belleza a sus clientas.

Devuelve SOLO un objeto JSON, sin texto alrededor:
{
  "name": "minusculas_con_guion_bajo",
  "category": "UTILITY" o "MARKETING",
  "body": "texto con {{1}}, {{2}}...",
  "footer": "texto corto o vacío",
  "buttons": ["texto botón", ...],
  "example_params": ["ejemplo para {{1}}", ...],
  "notes": "una o dos frases: por qué esta categoría y qué cuidar al usarla"
}

Reglas de Meta que NO se pueden romper:
- UTILITY solo si el mensaje es un servicio: sobre una cita, una cuenta, algo que la clienta pidió o un aviso operativo (horarios, cambios). Nada de promociones, descuentos, invitaciones a comprar ni "te extrañamos". Si hay algo promocional, es MARKETING, y dilo en notes.
- Variables {{1}}, {{2}}... consecutivas, empezando en 1. El cuerpo NO puede empezar ni terminar con una variable, y nunca dos variables seguidas.
- Poco peso de variables: el texto fijo debe ser mucho más que las variables. Nada de plantillas genéricas del tipo "Hola {{1}}, {{2}}": Meta las rechaza.
- Usa {{1}} para el nombre de la clienta cuando sirva saludarla, y solo las variables que de verdad cambian entre envíos.
- Cuerpo de máximo 1024 caracteres; pie de máximo 60, sin variables.
- Botones de respuesta rápida: máximo 3, de máximo 25 caracteres, SIN emojis. Úsalos cuando la clienta tenga que contestar algo (sí/no, confirmar); tocar uno abre la conversación.
- Emojis en el cuerpo con moderación (uno o dos).
- name: minúsculas, números y guion bajo, que describa el mensaje (ej: aviso_festivo_lunes).
"""


@dataclass
class TemplateDraft:
    name: str = ""
    category: str = "UTILITY"
    body: str = ""
    footer: str = ""
    buttons: list[str] = field(default_factory=list)
    example_params: list[str] = field(default_factory=list)
    notes: str = ""
    issues: list[str] = field(default_factory=list)
    attempts: int = 0


_EMOJI = re.compile("[\U0001F000-\U0001FAFF☀-➿️]")


def _as_draft(data: dict[str, Any]) -> TemplateDraft:
    buttons = data.get("buttons") or []
    return TemplateDraft(
        name=str(data.get("name") or "").strip(),
        category=str(data.get("category") or "UTILITY").strip().upper(),
        body=str(data.get("body") or "").strip(),
        footer=str(data.get("footer") or "").strip(),
        buttons=[str(b).strip() for b in buttons if str(b).strip()] if isinstance(buttons, list) else [],
        example_params=[str(p) for p in (data.get("example_params") or []) if str(p).strip()],
        notes=str(data.get("notes") or "").strip(),
    )


def validate_draft(draft: TemplateDraft) -> list[str]:
    """Lo que haria que Meta la rechace (o que no se pueda ni mandar)."""
    issues: list[str] = []
    if not re.fullmatch(r"[a-z0-9_]{3,512}", draft.name):
        issues.append("El nombre debe ir en minúsculas, números y guion bajo.")
    if draft.category not in ("UTILITY", "MARKETING"):
        issues.append("La categoría debe ser UTILITY o MARKETING.")
    if not draft.body:
        issues.append("Falta el cuerpo.")
    if len(draft.body) > MAX_BODY_CHARS:
        issues.append(f"El cuerpo pasa de {MAX_BODY_CHARS} caracteres.")

    numbers = [int(n) for n in re.findall(r"\{\{(\d+)\}\}", draft.body)]
    distinct = sorted(set(numbers))
    if distinct and distinct != list(range(1, len(distinct) + 1)):
        issues.append("Las variables deben ser {{1}}, {{2}}... consecutivas.")
    if re.match(r"^\s*\{\{\d+\}\}", draft.body) or re.search(r"\{\{\d+\}\}[\s.!?¡¿]*$", draft.body):
        issues.append("El cuerpo no puede empezar ni terminar con una variable.")
    if re.search(r"\{\{\d+\}\}\s*\{\{\d+\}\}", draft.body):
        issues.append("No puede haber dos variables seguidas.")
    fixed = re.sub(r"\{\{\d+\}\}", "", draft.body).strip()
    if distinct and len(fixed.split()) < 6 * len(distinct):
        issues.append("Demasiadas variables para tan poco texto fijo: Meta la rechazaría por genérica.")
    if len(draft.example_params) < len(distinct):
        issues.append("Falta un ejemplo por cada variable.")

    if len(draft.footer) > MAX_FOOTER_CHARS or "{{" in draft.footer:
        issues.append(f"El pie va sin variables y de máximo {MAX_FOOTER_CHARS} caracteres.")
    if len(draft.buttons) > MAX_BUTTONS:
        issues.append(f"Máximo {MAX_BUTTONS} botones.")
    for button in draft.buttons:
        if len(button) > MAX_BUTTON_CHARS:
            issues.append(f"El botón '{button}' pasa de {MAX_BUTTON_CHARS} caracteres.")
        if _EMOJI.search(button):
            issues.append(f"El botón '{button}' lleva emoji: Meta no lo acepta.")
    return issues


def to_components(draft: TemplateDraft) -> list[dict[str, Any]]:
    """El borrador en el formato de componentes que recibe Meta."""
    body: dict[str, Any] = {"type": "BODY", "text": draft.body}
    if draft.example_params:
        body["example"] = {"body_text": [draft.example_params]}
    components: list[dict[str, Any]] = [body]
    if draft.footer:
        components.append({"type": "FOOTER", "text": draft.footer})
    if draft.buttons:
        components.append(
            {"type": "BUTTONS", "buttons": [{"type": "QUICK_REPLY", "text": b} for b in draft.buttons]}
        )
    return components


def _user_prompt(description: str, business_name: str, category: str | None, previous: str | None, issues: list[str]) -> str:
    firma = (
        f"Negocio: {business_name}"
        if business_name
        else "Negocio: sin nombre. No inventes uno: si hace falta, di \"el salón\"."
    )
    parts = [firma, f"Lo que se quiere decir: {description.strip()}"]
    if category:
        parts.append(f"Categoría pedida: {category}. Si el contenido no cabe en ella, dilo en notes.")
    if previous and issues:
        parts.append("Tu propuesta anterior:\n" + previous)
        parts.append("Corrige esto y devuelve el JSON completo otra vez:\n- " + "\n- ".join(issues))
    return "\n\n".join(parts)


async def draft_template(
    description: str, *, business_name: str, category: str | None = None, business_id: str | None = None
) -> TemplateDraft:
    """@raise FlowGenerationUnavailable si ia-core no esta configurado o no responde."""
    draft = TemplateDraft()
    previous: str | None = None
    for _ in range(1 + MAX_RETRIES):
        draft_attempts = draft.attempts + 1
        text = await complete(
            SYSTEM_PROMPT,
            _user_prompt(description, business_name, category, previous, draft.issues),
            business_id=business_id,
        )
        data = extract_json(text)
        if data is None:
            draft = TemplateDraft(attempts=draft_attempts, issues=["La IA no devolvió una propuesta válida."])
            previous = text
            continue
        draft = _as_draft(data)
        draft.attempts = draft_attempts
        draft.issues = validate_draft(draft)
        previous = json.dumps(data, ensure_ascii=False, indent=2)
        if not draft.issues:
            break
    return draft
