from __future__ import annotations

import hashlib
import hmac

from nexolu_comms_api.core.webhooks.signing import build_forward_headers, verify_meta_signature


def test_verify_meta_signature_accepts_a_correctly_signed_body():
    body = b'{"entry": []}'
    secret = "meta-app-secret"
    signature = "sha256=" + hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()

    assert verify_meta_signature(body, signature, secret) is True


def test_verify_meta_signature_rejects_a_wrong_signature():
    body = b'{"entry": []}'

    assert verify_meta_signature(body, "sha256=deadbeef", "meta-app-secret") is False


def test_verify_meta_signature_rejects_a_missing_header():
    assert verify_meta_signature(b"{}", None, "meta-app-secret") is False


def test_verify_meta_signature_rejects_a_header_without_the_sha256_prefix():
    assert verify_meta_signature(b"{}", "deadbeef", "meta-app-secret") is False


def test_build_forward_headers_are_independently_verifiable():
    body = b'{"entry": []}'
    secret = "pos-callback-secret"

    headers = build_forward_headers(body, secret)

    expected = hmac.new(secret.encode(), f"{headers['X-Nexolu-Timestamp']}.".encode() + body, hashlib.sha256).hexdigest()
    assert headers["X-Nexolu-Signature"] == expected
