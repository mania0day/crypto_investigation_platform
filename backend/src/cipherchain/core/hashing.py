"""Content addressing for raw payloads and canonical JSON.

Raw provider payloads are stored and referenced by sha256 digest so that
every fact and finding can be traced back to the exact bytes that produced
it (vision §4, evidence provenance), and so the cache can deduplicate
immutable chain data.
"""

from __future__ import annotations

import hashlib
import json
from typing import Any


def sha256_hex(payload: bytes) -> str:
    """Hex sha256 digest of raw bytes."""
    return hashlib.sha256(payload).hexdigest()


def canonical_json_bytes(value: Any) -> bytes:
    """Deterministic JSON encoding: sorted keys, no whitespace, UTF-8.

    Two structurally equal values always produce identical bytes, so their
    digests match regardless of source key ordering.
    """
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode(
        "utf-8"
    )


def sha256_canonical_json(value: Any) -> str:
    """Hex sha256 of the canonical JSON encoding of ``value``."""
    return sha256_hex(canonical_json_bytes(value))
