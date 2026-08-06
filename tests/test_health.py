from __future__ import annotations


def test_health(client):
    response = client.get("/health")

    assert response.status_code == 200
    assert response.json() == {"status": "ok"}


def test_swagger_docs_are_served(client):
    assert client.get("/docs").status_code == 200
    assert client.get("/openapi.json").status_code == 200
