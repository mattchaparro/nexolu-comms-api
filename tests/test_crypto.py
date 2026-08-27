from __future__ import annotations

import pytest


def test_encrypted_string_round_trips(app_env):
    from nexolu_comms_api.core.security.crypto import _fernet

    token = _fernet().encrypt(b"super-secreto").decode()
    assert _fernet().decrypt(token.encode()).decode() == "super-secreto"


def test_encrypted_string_fails_closed_without_master_key(app_env, monkeypatch):
    from nexolu_comms_api.core.security.crypto import _fernet

    monkeypatch.delenv("COMMS_MASTER_KEY", raising=False)
    _fernet.cache_clear()

    with pytest.raises(RuntimeError, match="COMMS_MASTER_KEY"):
        _fernet()


def test_encrypted_json_round_trips_a_dict(app_env):
    from nexolu_comms_api.core.security.crypto import EncryptedJSON

    column = EncryptedJSON(4000)
    payload = {"access_token": "abc", "meta_app_secret": None, "n": 1}

    bound = column.process_bind_param(payload, dialect=None)
    assert bound != str(payload)  # esta realmente cifrado, no solo serializado
    assert column.process_result_value(bound, dialect=None) == payload


def test_encrypted_string_and_json_pass_through_none(app_env):
    from nexolu_comms_api.core.security.crypto import EncryptedJSON, EncryptedString

    assert EncryptedString(255).process_bind_param(None, dialect=None) is None
    assert EncryptedString(255).process_result_value(None, dialect=None) is None
    assert EncryptedJSON(4000).process_bind_param(None, dialect=None) is None
    assert EncryptedJSON(4000).process_result_value(None, dialect=None) is None
