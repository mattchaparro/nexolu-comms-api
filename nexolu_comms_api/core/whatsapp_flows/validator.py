"""Validador LOCAL del Flow JSON, antes de gastar una llamada a Meta.

Dos capas:

1. **Forma** (JSON Schema): version, pantallas, layout y el catalogo de
   componentes con sus propiedades obligatorias. Un nombre de componente
   mal escrito ("Textinput") se ataja aca con un mensaje claro en vez de
   un error de Meta con numero de linea.
2. **Reglas que un schema no expresa bien**, con mensaje en castellano:
   - `init-value` en un componente: Meta lo rechaza; el valor inicial va en
     `init-values` del Form (el error que nos costo el formulario del spa).
   - Toda pantalla terminal cierra con un Footer cuya accion es `complete`,
     y `complete` solo vive en pantallas terminales.
   - `navigate` apunta a pantallas que existen; `data_exchange` exige un
     endpoint que Connect todavia no tiene.
   - Titulos de botones y de las opciones de listas dentro del limite.
   - `${data.x}` declarado en `data` de la pantalla (con `__example__`, que
     es lo que llena la vista previa) y `${form.x}` con un campo que exista.

No pretende reemplazar al validador de Meta (que es el que decide): los
limites documentados que en la practica Meta no siempre aplica van como
`warning`, no como `error`, para no bloquear un JSON que Meta si acepta.
Fuente de los limites: developers.facebook.com/docs/whatsapp/flows/reference/components
"""
from __future__ import annotations

import re
from dataclasses import asdict, dataclass
from typing import Any

from jsonschema import Draft202012Validator

INPUT_COMPONENTS = {
    "TextInput",
    "TextArea",
    "CheckboxGroup",
    "RadioButtonsGroup",
    "OptIn",
    "Dropdown",
    "DatePicker",
    "CalendarPicker",
    "ChipsSelector",
    "PhotoPicker",
    "DocumentPicker",
}

# Propiedades obligatorias por componente (ademas de `type`).
REQUIRED_PROPERTIES: dict[str, list[str]] = {
    "TextHeading": ["text"],
    "TextSubheading": ["text"],
    "TextBody": ["text"],
    "TextCaption": ["text"],
    "RichText": ["text"],
    "TextInput": ["name", "label"],
    "TextArea": ["name", "label"],
    "CheckboxGroup": ["name", "label", "data-source"],
    "RadioButtonsGroup": ["name", "label", "data-source"],
    "Dropdown": ["name", "label", "data-source"],
    "ChipsSelector": ["name", "label", "data-source"],
    "OptIn": ["name", "label"],
    "DatePicker": ["name", "label"],
    "CalendarPicker": ["name", "label"],
    "PhotoPicker": ["name", "label"],
    "DocumentPicker": ["name", "label"],
    "Footer": ["label", "on-click-action"],
    "EmbeddedLink": ["text", "on-click-action"],
    "Image": ["src"],
    "ImageCarousel": ["images"],
    "NavigationList": ["name", "list-items"],
    "If": ["condition", "then"],
    "Switch": ["value", "cases"],
    "Form": ["name", "children"],
}

COMPONENT_TYPES = sorted(REQUIRED_PROPERTIES)

ACTION_NAMES = ["navigate", "complete", "data_exchange", "update_data", "open_url"]

# (componente, propiedad) -> (limite, severidad). Los `error` son los que
# Meta aplica siempre; los `warning`, limites documentados que no siempre
# hace cumplir (el formulario del spa paso con un label de TextInput de 42).
TEXT_LIMITS: dict[tuple[str, str], tuple[int, str]] = {
    ("Footer", "label"): (35, "error"),
    ("TextHeading", "text"): (80, "error"),
    ("TextSubheading", "text"): (80, "error"),
    ("TextCaption", "text"): (409, "error"),
    ("TextBody", "text"): (4096, "error"),
    ("TextInput", "label"): (20, "warning"),
    ("TextArea", "label"): (20, "warning"),
    ("CheckboxGroup", "label"): (30, "warning"),
    ("RadioButtonsGroup", "label"): (30, "warning"),
    ("Dropdown", "label"): (30, "warning"),
    ("DatePicker", "label"): (40, "warning"),
    ("CalendarPicker", "label"): (40, "warning"),
    ("OptIn", "label"): (120, "warning"),
    ("EmbeddedLink", "text"): (25, "error"),
}
OPTION_TITLE_LIMIT = 30
MAX_COMPONENTS_PER_SCREEN = 50

SCREEN_ID_PATTERN = r"^[A-Z][A-Z_]*$"
DYNAMIC_REF = re.compile(r"\$\{(data|form)\.([A-Za-z0-9_]+)")


def _component_schema() -> dict[str, Any]:
    return {
        "type": "object",
        "required": ["type"],
        "properties": {
            "type": {"enum": COMPONENT_TYPES},
            # Contenedores: Form (children), If (then/else), Switch (cases).
            "children": {"type": "array", "items": {"$ref": "#/$defs/component"}},
            "then": {"type": "array", "items": {"$ref": "#/$defs/component"}},
            "else": {"type": "array", "items": {"$ref": "#/$defs/component"}},
            "cases": {
                "type": "object",
                "additionalProperties": {"type": "array", "items": {"$ref": "#/$defs/component"}},
            },
        },
        "allOf": [
            {
                "if": {"properties": {"type": {"const": name}}, "required": ["type"]},
                "then": {"required": props},
            }
            for name, props in REQUIRED_PROPERTIES.items()
        ],
    }


FLOW_JSON_SCHEMA: dict[str, Any] = {
    "$schema": "https://json-schema.org/draft/2020-12/schema",
    "type": "object",
    "required": ["version", "screens"],
    "additionalProperties": False,
    "properties": {
        "version": {"type": "string", "pattern": r"^\d+\.\d+$"},
        "data_api_version": {"type": "string"},
        "routing_model": {"type": "object"},
        "screens": {
            "type": "array",
            "minItems": 1,
            "items": {
                "type": "object",
                "required": ["id", "layout"],
                "additionalProperties": False,
                "properties": {
                    "id": {"type": "string", "pattern": SCREEN_ID_PATTERN},
                    "title": {"type": "string"},
                    "terminal": {"type": "boolean"},
                    "success": {"type": "boolean"},
                    "refresh_on_back": {"type": "boolean"},
                    "sensitive": {"type": "array", "items": {"type": "string"}},
                    "data": {"type": "object"},
                    "layout": {
                        "type": "object",
                        "required": ["type", "children"],
                        "properties": {
                            "type": {"const": "SingleColumnLayout"},
                            "children": {"type": "array", "items": {"$ref": "#/$defs/component"}},
                        },
                    },
                },
            },
        },
    },
    "$defs": {"component": _component_schema()},
}

_schema_validator = Draft202012Validator(FLOW_JSON_SCHEMA)


@dataclass
class FlowIssue:
    """Un problema del Flow JSON. `source`: local | meta."""

    message: str
    path: str = ""
    severity: str = "error"
    source: str = "local"
    # Linea del JSON subido (los errores de Meta la traen; el panel salta
    # ahi en el editor, que muestra el JSON con la misma indentacion).
    line: int | None = None

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def validate_flow_json(flow_json: Any) -> list[FlowIssue]:
    """@return errores y advertencias; vacia = listo para subir a Meta."""
    if not isinstance(flow_json, dict):
        return [FlowIssue("El Flow JSON tiene que ser un objeto.")]

    issues = [_schema_issue(error) for error in _schema_validator.iter_errors(flow_json)]
    if issues:
        # Con la forma rota, las reglas de abajo darian ruido encima.
        return sorted(issues, key=lambda issue: issue.path)

    return _SemanticChecker(flow_json).run()


def has_errors(issues: list[FlowIssue]) -> bool:
    return any(issue.severity == "error" for issue in issues)


def _schema_issue(error: Any) -> FlowIssue:
    path = _format_path(error.absolute_path)
    validator = error.validator
    if validator == "enum" and error.absolute_path and error.absolute_path[-1] == "type":
        message = (
            f"Componente desconocido '{error.instance}'. Validos: {', '.join(COMPONENT_TYPES)}."
        )
    elif validator == "required":
        missing = error.message.split("'")[1] if "'" in error.message else error.message
        message = f"Falta la propiedad obligatoria '{missing}'."
    elif validator == "additionalProperties":
        message = f"Propiedad no permitida: {error.message.split('(')[-1].rstrip(')')}"
    elif validator == "pattern" and path.endswith(".id"):
        message = "El id de la pantalla va en MAYUSCULAS y guion bajo (ej: CONFIRMAR_CITA)."
    elif validator == "pattern" and path == "version":
        message = "La version va como texto 'mayor.menor' (ej: \"7.2\")."
    else:
        message = error.message
    return FlowIssue(message=message, path=path)


def _format_path(parts: Any) -> str:
    text = ""
    for part in parts:
        text += f"[{part}]" if isinstance(part, int) else (f".{part}" if text else str(part))
    return text


class _SemanticChecker:
    def __init__(self, flow_json: dict[str, Any]) -> None:
        self._flow = flow_json
        self._screens: list[dict[str, Any]] = flow_json["screens"]
        self._screen_ids = [screen["id"] for screen in self._screens]
        self._issues: list[FlowIssue] = []

    def run(self) -> list[FlowIssue]:
        seen: set[str] = set()
        for index, screen in enumerate(self._screens):
            base = f"screens[{index}]"
            if screen["id"] in seen:
                self._error(f"Hay dos pantallas con el id '{screen['id']}'.", f"{base}.id")
            seen.add(screen["id"])
            if screen["id"] == "SUCCESS":
                self._error("'SUCCESS' es un id reservado por Meta.", f"{base}.id")
            self._check_screen(screen, base)

        if not any(screen.get("terminal") for screen in self._screens):
            self._error("Ninguna pantalla es terminal (\"terminal\": true): el formulario no cierra.")
        if "data_api_version" in self._flow or "routing_model" in self._flow:
            self._error(
                "data_api_version/routing_model son de formularios con endpoint (data_exchange), "
                "que Connect todavia no soporta."
            )
        return self._issues

    # -- por pantalla ---------------------------------------------------------

    def _check_screen(self, screen: dict[str, Any], base: str) -> None:
        data = screen.get("data") or {}
        for key, spec in data.items():
            if not isinstance(spec, dict) or "type" not in spec:
                self._error(f"El dato '{key}' necesita un 'type'.", f"{base}.data.{key}")
            elif "__example__" not in spec:
                self._error(
                    f"El dato '{key}' necesita '__example__' (Meta lo usa para la vista previa).",
                    f"{base}.data.{key}",
                )

        components = list(self._walk(screen["layout"]["children"], f"{base}.layout.children"))
        if len(components) > MAX_COMPONENTS_PER_SCREEN:
            self._error(
                f"La pantalla tiene {len(components)} componentes; Meta permite {MAX_COMPONENTS_PER_SCREEN}.",
                base,
            )

        form_names: set[str] = set()
        for component, path, _ in components:
            name = component.get("name")
            if component["type"] in INPUT_COMPONENTS and isinstance(name, str):
                if name in form_names:
                    self._error(f"Hay dos campos con el name '{name}' en la pantalla.", f"{path}.name")
                form_names.add(name)

        top_level_footers = [c for c, _, nested in components if c["type"] == "Footer" and not nested]
        if len(top_level_footers) > 1:
            self._error("Una pantalla admite un solo Footer.", base)

        actions: list[tuple[dict[str, Any], str]] = []
        for component, path, _ in components:
            self._check_component(component, path, form_names)
            for key in ("on-click-action", "on-select-action", "on-unselect-action"):
                action = component.get(key)
                if isinstance(action, dict):
                    actions.append((action, f"{path}.{key}"))

        for action, path in actions:
            self._check_action(action, path, screen)

        if screen.get("terminal"):
            has_footer = any(c["type"] == "Footer" for c, _, _ in components)
            completes = any(a.get("name") == "complete" for a, _ in actions)
            if not has_footer:
                self._error(
                    "Una pantalla terminal necesita un Footer (el boton que cierra el formulario).",
                    base,
                )
            elif not completes:
                self._error(
                    "El Footer de la pantalla terminal tiene que usar on-click-action "
                    "\"complete\" (es lo que le entrega las respuestas al negocio).",
                    base,
                )

        declared = set(data)
        for ref_kind, ref_name, path in self._refs(screen["layout"], f"{base}.layout"):
            if ref_kind == "data" and ref_name not in declared:
                self._error(
                    f"'${{data.{ref_name}}}' no esta declarado en \"data\" de la pantalla.", path
                )
            if ref_kind == "form" and ref_name not in form_names:
                self._error(
                    f"'${{form.{ref_name}}}' no corresponde a ningun campo de la pantalla.", path
                )

    def _walk(self, children: Any, path: str, nested: bool = False):
        """Recorre componentes, entrando a Form/If/Switch. @yield
        (componente, ruta, si esta dentro de una rama If/Switch)."""
        if not isinstance(children, list):
            return
        for index, component in enumerate(children):
            if not isinstance(component, dict):
                continue
            here = f"{path}[{index}]"
            yield component, here, nested
            kind = component.get("type")
            if kind == "Form":
                yield from self._walk(component.get("children"), f"{here}.children", nested)
            elif kind == "If":
                yield from self._walk(component.get("then"), f"{here}.then", True)
                yield from self._walk(component.get("else"), f"{here}.else", True)
            elif kind == "Switch" and isinstance(component.get("cases"), dict):
                for case, branch in component["cases"].items():
                    yield from self._walk(branch, f"{here}.cases.{case}", True)

    def _check_component(self, component: dict[str, Any], path: str, form_names: set[str]) -> None:
        kind = component["type"]
        if "init-value" in component:
            self._error(
                "Meta rechaza 'init-value' en el componente: el valor inicial va en "
                "\"init-values\" del Form ({\"<name>\": \"...\"}).",
                f"{path}.init-value",
            )
        if kind == "Form":
            init_values = component.get("init-values")
            if isinstance(init_values, dict):
                inner = {
                    c.get("name")
                    for c, _, _ in self._walk(component.get("children"), path)
                    if c.get("type") in INPUT_COMPONENTS
                }
                for key in init_values:
                    if key not in inner:
                        self._error(
                            f"init-values tiene '{key}', que no es un campo de este Form.",
                            f"{path}.init-values.{key}",
                        )

        for prop in ("label", "text"):
            limit = TEXT_LIMITS.get((kind, prop))
            value = component.get(prop)
            if limit and isinstance(value, str) and not value.startswith("${"):
                max_len, severity = limit
                if len(value) > max_len:
                    self._issues.append(
                        FlowIssue(
                            f"{kind}.{prop} tiene {len(value)} caracteres; Meta permite {max_len}.",
                            f"{path}.{prop}",
                            severity,
                        )
                    )

        options = component.get("data-source")
        if isinstance(options, list):
            if not options:
                self._error("La lista de opciones esta vacia.", f"{path}.data-source")
            for index, option in enumerate(options):
                option_path = f"{path}.data-source[{index}]"
                if not isinstance(option, dict) or not option.get("id") or not option.get("title"):
                    self._error("Cada opcion necesita 'id' y 'title'.", option_path)
                elif len(str(option["title"])) > OPTION_TITLE_LIMIT:
                    self._error(
                        f"El titulo '{option['title']}' pasa de {OPTION_TITLE_LIMIT} caracteres.",
                        f"{option_path}.title",
                    )

    def _check_action(self, action: dict[str, Any], path: str, screen: dict[str, Any]) -> None:
        name = action.get("name")
        if name not in ACTION_NAMES:
            self._error(f"Accion desconocida '{name}'. Validas: {', '.join(ACTION_NAMES)}.", path)
            return
        if name == "complete" and not screen.get("terminal"):
            self._error("La accion 'complete' solo va en una pantalla terminal.", path)
        if name == "data_exchange":
            self._error(
                "'data_exchange' necesita un endpoint cifrado que Connect todavia no tiene; "
                "usa 'navigate' o 'complete'.",
                path,
            )
        if name == "navigate":
            target = (action.get("next") or {}).get("name")
            if target not in self._screen_ids:
                self._error(f"'navigate' apunta a la pantalla '{target}', que no existe.", path)
            elif target == screen["id"]:
                self._error("'navigate' no puede volver a la misma pantalla.", path)
            else:
                target_data = next(s for s in self._screens if s["id"] == target).get("data") or {}
                for key in (action.get("payload") or {}):
                    if key not in target_data:
                        self._issues.append(
                            FlowIssue(
                                f"El payload manda '{key}', que la pantalla '{target}' no declara en \"data\".",
                                f"{path}.payload.{key}",
                                "warning",
                            )
                        )

    def _refs(self, node: Any, path: str):
        """Todas las referencias ${data.x} / ${form.x} bajo `node`."""
        if isinstance(node, dict):
            for key, value in node.items():
                yield from self._refs(value, f"{path}.{key}")
        elif isinstance(node, list):
            for index, value in enumerate(node):
                yield from self._refs(value, f"{path}[{index}]")
        elif isinstance(node, str):
            for match in DYNAMIC_REF.finditer(node):
                yield match.group(1), match.group(2), path

    def _error(self, message: str, path: str = "") -> None:
        self._issues.append(FlowIssue(message, path))


def meta_issues(validation_errors: list[dict[str, Any]]) -> list[FlowIssue]:
    """Normaliza los validation_errors de Meta al mismo formato."""
    issues = []
    for error in validation_errors:
        where = ""
        if error.get("line_start"):
            where = f"linea {error['line_start']}, col {error.get('column_start', '?')}"
        pointers = error.get("pointers") or []
        if pointers and pointers[0].get("path"):
            where = pointers[0]["path"] + (f" ({where})" if where else "")
        label = error.get("error") or error.get("error_type") or "ERROR"
        issues.append(
            FlowIssue(
                message=f"{label}: {error.get('message') or 'sin detalle'}",
                path=where,
                source="meta",
                line=int(error["line_start"]) if error.get("line_start") else None,
            )
        )
    return issues
