from __future__ import annotations

from nexolu_comms_api.core.security.api_keys import generate_api_key, hash_api_key


def test_generate_api_key_has_expected_prefix_and_is_random():
    first = generate_api_key()
    second = generate_api_key()

    assert first.startswith("ncm_")
    assert first != second


def test_hash_api_key_is_deterministic_and_one_way():
    key = "ncm_example"

    assert hash_api_key(key) == hash_api_key(key)
    assert hash_api_key(key) != key
    assert len(hash_api_key(key)) == 64  # hex sha256
