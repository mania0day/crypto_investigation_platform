from cipherchain.core.hashing import canonical_json_bytes, sha256_canonical_json, sha256_hex


def test_sha256_hex_is_deterministic() -> None:
    assert sha256_hex(b"cipherchain") == sha256_hex(b"cipherchain")
    assert len(sha256_hex(b"cipherchain")) == 64


def test_canonical_json_ignores_key_order() -> None:
    a = {"b": 1, "a": [1, 2], "c": {"y": None, "x": "€"}}
    b = {"c": {"x": "€", "y": None}, "a": [1, 2], "b": 1}
    assert canonical_json_bytes(a) == canonical_json_bytes(b)
    assert sha256_canonical_json(a) == sha256_canonical_json(b)


def test_canonical_json_distinguishes_values() -> None:
    assert sha256_canonical_json({"a": 1}) != sha256_canonical_json({"a": 2})
