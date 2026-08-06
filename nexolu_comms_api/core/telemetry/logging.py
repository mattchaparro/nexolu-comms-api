"""Logging estructurado en JSON.

Un log por linea, en JSON, es lo que cualquier agregador (CloudWatch, Loki,
lo que sea) espera sin transformacion. No hay nada especifico de un producto
aca: cada log que le importa a auditoria (que se envio, a que tenant) ya
sale como campo `extra`, no como texto libre para parsear despues.
"""
from __future__ import annotations

import json
import logging
import sys
from datetime import UTC, datetime


class JsonFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        payload = {
            "timestamp": datetime.fromtimestamp(record.created, tz=UTC).isoformat(),
            "level": record.levelname,
            "logger": record.name,
            "message": record.getMessage(),
        }

        if record.exc_info:
            payload["exception"] = self.formatException(record.exc_info)

        standard_keys = logging.LogRecord("", 0, "", 0, "", None, None).__dict__.keys()
        for key, value in record.__dict__.items():
            if key not in standard_keys and key not in payload:
                payload[key] = value

        return json.dumps(payload, default=str, ensure_ascii=False)


def configure_logging(level: str = "INFO") -> None:
    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(JsonFormatter())

    root = logging.getLogger()
    root.handlers = [handler]
    root.setLevel(level)
