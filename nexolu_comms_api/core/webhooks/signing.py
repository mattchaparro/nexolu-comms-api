"""Dos esquemas de firma distintos, para las dos direcciones del webhook:

- `verify_meta_signature`: Meta firma cada POST con `X-Hub-Signature-256`
  (HMAC-SHA256 del cuerpo crudo con el App Secret del dashboard de Meta).
  Prueba que el evento de verdad vino de Meta.
- `sign_forward`/`build_forward_headers`: este servicio firma lo que
  REENVIA a la app cliente, mismo patron que ya usa Nexolu Payments Core
  con sus apps (timestamp + "." + cuerpo, HMAC-SHA256 con el
  `callback_secret` propio de esa app) - la app cliente ya sabe verificar
  esto (ver PaymentsCoreWebhookController::hasValidSignature() en el POS).
"""
from __future__ import annotations

import hashlib
import hmac
import time


def verify_meta_signature(body: bytes, header_value: str | None, app_secret: str) -> bool:
    if not header_value or not header_value.startswith("sha256="):
        return False

    expected = hmac.new(app_secret.encode(), body, hashlib.sha256).hexdigest()

    return hmac.compare_digest(header_value.removeprefix("sha256="), expected)


def build_forward_headers(body: bytes, secret: str) -> dict[str, str]:
    timestamp = str(int(time.time()))
    signed = f"{timestamp}.".encode() + body
    signature = hmac.new(secret.encode(), signed, hashlib.sha256).hexdigest()

    return {"X-Nexolu-Timestamp": timestamp, "X-Nexolu-Signature": signature}
