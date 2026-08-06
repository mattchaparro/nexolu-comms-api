from __future__ import annotations


def _send_email(client, auth_headers, httpx_mock, business_id="1", reference=None):
    httpx_mock.add_response(url="https://api.brevo.com/v3/smtp/email", json={"messageId": "<m@brevo>"})
    return client.post(
        "/v1/notifications/send",
        headers=auth_headers,
        json={
            "business_id": business_id,
            "reference": reference,
            "channels": ["email"],
            "to": {"email": "a@b.com"},
            "subject": "Hola",
            "text": "hola",
        },
    )


def test_usage_summary_requires_authorization(client):
    assert client.get("/v1/usage/summary").status_code == 401


def test_usage_summary_counts_only_sent_notifications(client, auth_headers, httpx_mock):
    _send_email(client, auth_headers, httpx_mock, business_id="1")

    response = client.get("/v1/usage/summary", headers=auth_headers)

    assert response.status_code == 200
    assert response.json()["summary"]["message_count"] == 1


def test_usage_summary_breaks_down_by_business_when_unfiltered(client, auth_headers, httpx_mock):
    _send_email(client, auth_headers, httpx_mock, business_id="1")
    _send_email(client, auth_headers, httpx_mock, business_id="2")

    response = client.get("/v1/usage/summary", headers=auth_headers)

    by_business = {row["key"]: row["message_count"] for row in response.json()["by_business"]}
    assert by_business == {"1": 1, "2": 1}


def test_usage_summary_filtered_to_one_business_omits_the_breakdown(client, auth_headers, httpx_mock):
    _send_email(client, auth_headers, httpx_mock, business_id="1")

    response = client.get("/v1/usage/summary?business_id=1", headers=auth_headers)

    assert response.json()["summary"]["message_count"] == 1
    assert response.json()["by_business"] is None


def test_usage_daily_returns_one_point_per_day(client, auth_headers, httpx_mock):
    _send_email(client, auth_headers, httpx_mock, business_id="1")

    response = client.get("/v1/usage/daily", headers=auth_headers)

    days = response.json()["days"]
    assert len(days) == 1
    assert days[0]["message_count"] == 1


def test_platform_usage_requires_the_platform_key(client, auth_headers, httpx_mock):
    _send_email(client, auth_headers, httpx_mock, business_id="1")

    assert client.get("/v1/platform/usage").status_code == 401
    assert client.get("/v1/platform/usage", headers=auth_headers).status_code == 401


def test_platform_usage_is_disabled_without_a_configured_key(client, monkeypatch):
    monkeypatch.delenv("NEXOLU_PLATFORM_API_KEY", raising=False)
    from nexolu_comms_api.config import get_settings

    get_settings.cache_clear()

    response = client.get("/v1/platform/usage", headers={"Authorization": "Bearer whatever"})

    assert response.status_code == 503


def test_platform_usage_breaks_down_by_app(client, auth_headers, httpx_mock):
    _send_email(client, auth_headers, httpx_mock, business_id="1")

    response = client.get("/v1/platform/usage", headers={"Authorization": "Bearer platform-key"})

    assert response.status_code == 200
    by_app = {row["key"]: row["message_count"] for row in response.json()["breakdown"]}
    assert by_app == {"pos": 1}


def test_platform_usage_filtered_to_an_app_breaks_down_by_business(client, auth_headers, httpx_mock):
    _send_email(client, auth_headers, httpx_mock, business_id="1")
    _send_email(client, auth_headers, httpx_mock, business_id="2")

    response = client.get("/v1/platform/usage?app_id=pos", headers={"Authorization": "Bearer platform-key"})

    by_business = {row["key"]: row["message_count"] for row in response.json()["breakdown"]}
    assert by_business == {"1": 1, "2": 1}
