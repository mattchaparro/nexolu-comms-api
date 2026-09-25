"""Un numero que el negocio dejo de usar contesta a donde escribir.

Luxury paso del 304 al 301. Quien le sigue escribiendo al 304 no recibia
nada: la app contesta desde el 301, y a quien no le escribio al 301 eso
no le llega. Ahora el 304 mismo le dice a donde escribir.
"""
from __future__ import annotations

import hashlib
import hmac
import json

GRAPH_OLD = "https://graph.facebook.com/v21.0/999000/messages"


def _to_number(client, phone_number_id: str, text: str = "Hola, quiero cita"):
    body = {"entry": [{"changes": [{"field": "messages", "value": {
        "metadata": {"phone_number_id": phone_number_id},
        "contacts": [{"profile": {"name": "Clienta"}, "wa_id": "573001112233"}],
        "messages": [{"from": "573001112233", "id": f"wamid.{abs(hash(text))}", "type": "text", "text": {"body": text}}],
    }}]}]}
    raw = json.dumps(body).encode()
    signature = "sha256=" + hmac.new(b"meta-app-secret", raw, hashlib.sha256).hexdigest()
    return client.post("/webhooks/whatsapp/pos", content=raw,
                       headers={"Content-Type": "application/json", "X-Hub-Signature-256": signature})


def test_el_numero_retirado_contesta_desde_si_mismo_y_no_pasa_a_la_app(client, httpx_mock, monkeypatch):
    monkeypatch.setenv("RETIRED_NUMBER_REPLIES", json.dumps({"999000": {
        "text": "Ya no usamos este numero, escribenos al 301 948 9912",
        "cta_url": "https://wa.me/573019489912", "cta_title": "Escribir al 301",
    }}))
    from nexolu_comms_api.config import get_settings

    get_settings.cache_clear()
    httpx_mock.add_response(url=GRAPH_OLD, json={"messages": [{"id": "wamid.aviso"}]})

    assert _to_number(client, "999000").status_code == 200
    # Una rafaga: se contesta una sola vez.
    assert _to_number(client, "999000", "sigo aca?").status_code == 200

    sent = httpx_mock.get_requests(url=GRAPH_OLD)
    assert len(sent) == 1
    body = json.loads(sent[0].content)
    assert body["to"] == "573001112233"
    assert "301 948 9912" in json.dumps(body, ensure_ascii=False)
    assert "wa.me/573019489912" in json.dumps(body)
    # Y nada va al callback de la app: el bot contestaria desde el numero nuevo.
    assert httpx_mock.get_requests(url="https://pos.nexolu.test/webhooks/nexolu-comms/whatsapp") == []


def test_sin_configuracion_todo_sigue_igual(client, httpx_mock):
    httpx_mock.add_response(url="https://pos.nexolu.test/webhooks/nexolu-comms/whatsapp", json={"ok": True})
    assert _to_number(client, "123456").status_code == 200
    assert httpx_mock.get_requests(url=GRAPH_OLD) == []
