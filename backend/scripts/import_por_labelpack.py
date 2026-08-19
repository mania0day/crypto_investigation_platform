#!/usr/bin/env python
"""Turn an exchange proof-of-reserves file into a labelpack of VERIFIED addresses.

Exchanges that publish proof of reserves disclose the addresses holding user
funds. The strongest of these disclosures are *self-attesting*: OKX publishes
each address alongside a signature over the message "I am an OKX address", so
control of the key can be checked by anyone, with no trust in OKX, in an
explorer, or in a label vendor.

This script consumes such a file and emits a labelpack containing **only the
entries whose signature actually recovers to the claimed address**. An entry
that fails verification is reported and dropped — never shipped with a caveat.
That is the same standard `labels/verified-mixers.json`, `bridges/` and
`assets/` were built to: CipherChain ships nothing it has not confirmed.

Usage
-----
    # 1. Download the exchange's published file (browser; the endpoints sit
    #    behind bot protection and cannot be fetched from a script).
    #      OKX: https://www.okx.com/proof-of-reserves/download
    # 2. Verify it and emit the pack:
    python scripts/import_por_labelpack.py \\
        --csv ~/Downloads/okx_por.csv \\
        --entity "OKX" \\
        --source-url https://www.okx.com/proof-of-reserves \\
        --out ../labels/verified-vasps.json

Requires the `scripts` extra:  pip install -e '.[scripts]'

Scope, stated plainly
---------------------
EVM, Tron, Bitcoin (2-of-3 multisig) and Solana (ed25519) signatures are
verified here. Rows on chains this script cannot check (Bitcoin script
types, TON, Aptos, …) are reported as UNVERIFIED and dropped, because
shipping them would mean asserting something this tool did not establish.

Note also what a proof-of-reserves address IS: a wallet holding reserves.
Deposit-collection wallets and withdrawal hot wallets may differ from reserve
wallets, so a pack built this way answers "these funds reached <entity>"
better than it answers "this is the wallet user deposits are swept into".
"""

from __future__ import annotations

import argparse
import base64
import contextlib
import csv
import hashlib
import json
import sys
from collections import Counter
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

try:
    from eth_account import Account
    from eth_account.messages import encode_defunct
    from eth_keys.datatypes import Signature
    from eth_utils import keccak
except ImportError:  # pragma: no cover - dependency guard
    print(
        "error: this script needs the 'scripts' extra — pip install -e '.[scripts]'",
        file=sys.stderr,
    )
    raise SystemExit(2) from None

# Chains whose signatures this script can actually check, mapped from the
# network names proof-of-reserves files use to CipherChain chain ids.
EVM_NETWORKS = {
    "ETH": "ethereum",
    "ETHEREUM": "ethereum",
    "POLYGON": "polygon",
    "MATIC": "polygon",
}
TRON_NETWORKS = {"TRON": "tron", "TRX": "tron"}
BTC_NETWORKS = {"BTC": "bitcoin"}
SOL_NETWORKS = {"SOL": "solana", "SOLANA": "solana"}

_TRON_PREFIX = b"\x41"  # mainnet version byte, prepended before base58check
_B58 = "123456789ABCDEFGHJKLMNPQRSTUVWXYZabcdefghijkmnopqrstuvwxyz"


def _base58check(payload: bytes) -> str:
    """Bitcoin-style base58check with a double-SHA256 checksum."""
    full = payload + hashlib.sha256(hashlib.sha256(payload).digest()).digest()[:4]
    number = int.from_bytes(full, "big")
    out = ""
    while number:
        number, remainder = divmod(number, 58)
        out = _B58[remainder] + out
    return "1" * (len(full) - len(full.lstrip(b"\x00"))) + out


def verify_tron(address: str, message: str, signature: str) -> bool:
    """Does this signature prove control of the key behind a Tron address?

    Tron signs::

        keccak256( "\\x19TRON Signed Message:\\n" + len(message) + message )

    which is the ``HashTrxMsgV2`` variant in OKX's verifier — NOT the
    ``TronMessageSignatureHeader`` ("…\\n32" over a keccak digest) that the
    coin-to-header map points at. Both are tried there and only the V2 form
    matches the published data; the V1 form recovers a valid but wrong address
    for every row, exactly as the EVM text-vs-digest mistake did.

    Recovery yields a 20-byte account hash — the same value an Ethereum address
    is — which Tron renders as base58check over ``0x41 || hash``.
    """
    try:
        payload = b"\x19TRON Signed Message:\n" + str(len(message)).encode() + message.encode()
        recovered = Account._recover_hash(keccak(payload), signature=signature)
        derived = _base58check(_TRON_PREFIX + bytes.fromhex(recovered[2:]))
    except Exception:
        return False
    return derived == address.strip()


# ── Bitcoin ──────────────────────────────────────────────────────────────────
# OKX's BTC reserves are 2-of-3 multisig: two base64 compact signatures plus the
# redeem script. Verifying means BOTH halves — each signature must recover to a
# pubkey that appears in the script, AND the script must hash to the claimed
# address. Either alone proves nothing: signatures without the address link say
# nothing about THIS address, and the address link without signatures says
# nothing about key control.

B58 = "123456789ABCDEFGHJKLMNPQRSTUVWXYZabcdefghijkmnopqrstuvwxyz"
BECH32_CHARSET = "qpzry9x8gf2tvdw0s3jn54khce6mua7l"


def _sha256d(b: bytes) -> bytes:
    return hashlib.sha256(hashlib.sha256(b).digest()).digest()


def _hash160(b: bytes) -> bytes:
    return hashlib.new("ripemd160", hashlib.sha256(b).digest()).digest()


def _varint(n: int) -> bytes:
    if n < 0xFD:
        return bytes([n])
    if n <= 0xFFFF:
        return b"\xfd" + n.to_bytes(2, "little")
    return b"\xfe" + n.to_bytes(4, "little")


def _btc_digest(message: str, header: str = "Bitcoin Signed Message:\n") -> bytes:
    h, m = header.encode(), message.encode()
    return _sha256d(_varint(len(h)) + h + _varint(len(m)) + m)


def _b58check(payload: bytes) -> str:
    full = payload + _sha256d(payload)[:4]
    n = int.from_bytes(full, "big")
    out = ""
    while n:
        n, r = divmod(n, 58)
        out = B58[r] + out
    return "1" * (len(full) - len(full.lstrip(b"\x00"))) + out


def _bech32_polymod(values):
    gen = [0x3B6A57B2, 0x26508E6D, 0x1EA119FA, 0x3D4233DD, 0x2A1462B3]
    chk = 1
    for v in values:
        top = chk >> 25
        chk = ((chk & 0x1FFFFFF) << 5) ^ v
        for i in range(5):
            chk ^= gen[i] if ((top >> i) & 1) else 0
    return chk


def _bech32_encode(hrp: str, data, spec_const: int) -> str:
    values = [ord(c) >> 5 for c in hrp] + [0] + [ord(c) & 31 for c in hrp] + data
    polymod = _bech32_polymod([*values, 0, 0, 0, 0, 0, 0]) ^ spec_const
    checksum = [(polymod >> 5 * (5 - i)) & 31 for i in range(6)]
    return hrp + "1" + "".join(BECH32_CHARSET[d] for d in data + checksum)


def _convertbits(data, frombits, tobits, pad=True):
    acc = bits = 0
    ret = []
    maxv = (1 << tobits) - 1
    for value in data:
        acc = (acc << frombits) | value
        bits += frombits
        while bits >= tobits:
            bits -= tobits
            ret.append((acc >> bits) & maxv)
    if pad and bits:
        ret.append((acc << (tobits - bits)) & maxv)
    return ret


def _recover_pubkey(digest: bytes, sig_b64: str) -> bytes | None:
    """Compact recoverable signature: 1 header byte + r(32) + s(32).

    The header encodes the recovery id and whether the pubkey was compressed:
    27..30 uncompressed, 31..34 compressed, plus segwit variants at 35..42.
    """
    try:
        raw = base64.b64decode(sig_b64)
    except Exception:
        return None
    if len(raw) != 65:
        return None
    header = raw[0]
    recid = (header - 27) & 0x03
    compressed = ((header - 27) & 0x04) != 0 or header >= 39
    try:
        sig = Signature(
            vrs=(recid, int.from_bytes(raw[1:33], "big"), int.from_bytes(raw[33:65], "big"))
        )
        pub = sig.recover_public_key_from_msg_hash(digest)
    except Exception:
        return None
    body = pub.to_bytes()  # 64 bytes, uncompressed without the 0x04 prefix
    if compressed:
        prefix = b"\x03" if body[63] & 1 else b"\x02"
        return prefix + body[:32]
    return b"\x04" + body


def _address_from_script(script: bytes, claimed: str) -> str | None:
    if claimed.startswith("bc1"):
        program = hashlib.sha256(script).digest()  # P2WSH
        return _bech32_encode("bc", [0, *_convertbits(program, 8, 5)], 1)
    return _b58check(b"\x05" + _hash160(script))  # P2SH


def verify_bitcoin(address: str, message: str, sig1: str, sig2: str, script_hex: str) -> bool:
    address = address.strip()
    if not script_hex:
        return False
    try:
        script = bytes.fromhex(script_hex.strip())
    except ValueError:
        return False
    if _address_from_script(script, address) != address:
        return False
    digest = _btc_digest(message)
    signatures = [s for s in (sig1, sig2) if s]
    if not signatures:
        return False
    for signature in signatures:
        pubkey = _recover_pubkey(digest, signature)
        # `b"" in script` is True for every script, so a failed recovery must be
        # rejected explicitly — falling back to empty bytes would accept any
        # malformed signature while looking like a strict membership check.
        if not pubkey or pubkey not in script:
            return False
    return True


# ── Solana ───────────────────────────────────────────────────────────────────
# A Solana address IS the ed25519 public key, base58-encoded (plain base58, no
# checksum) — so verification needs no recovery step at all: decode the
# address, verify the signature over the message with that key.


def _b58decode(text: str) -> bytes | None:
    number = 0
    for char in text:
        index = _B58.find(char)
        if index < 0:
            return None
        number = number * 58 + index
    decoded = number.to_bytes((number.bit_length() + 7) // 8, "big")
    return b"\x00" * (len(text) - len(text.lstrip("1"))) + decoded


def verify_solana(address: str, message: str, signature: str) -> bool:
    """Does this signature verify under the key the address IS?

    The signature encoding in the published file is not pinned by a spec the
    way EIP-191 is, so base64, base58 and hex are each tried — a 64-byte
    ed25519 signature is unambiguous once decoded, and the verifying key is
    fixed by the address, so accepting several ENCODINGS cannot accept a
    wrong SIGNATURE. The message is signed as its raw UTF-8 bytes (the plain
    ed25519 convention); if OKX's file uses a digest variant instead, every
    row simply fails closed and is dropped — reported, never shipped.
    """
    try:
        from cryptography.exceptions import InvalidSignature
        from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey
    except ImportError:
        print(
            "error: Solana verification needs the 'scripts' extra — pip install -e '.[scripts]'",
            file=sys.stderr,
        )
        raise SystemExit(2) from None

    pubkey = _b58decode(address.strip())
    if pubkey is None or len(pubkey) != 32:
        return False
    candidates: list[bytes] = []
    with contextlib.suppress(Exception):
        candidates.append(base64.b64decode(signature, validate=True))
    decoded = _b58decode(signature.strip())
    if decoded is not None:
        candidates.append(decoded)
    with contextlib.suppress(ValueError):
        candidates.append(bytes.fromhex(signature.strip().removeprefix("0x")))
    key = Ed25519PublicKey.from_public_bytes(pubkey)
    for raw in candidates:
        if len(raw) != 64:
            continue
        try:
            key.verify(raw, message.encode())
            return True
        except InvalidSignature:
            continue
    return False


def canonical_address(chain: str, address: str) -> str:
    """Base58 chains are case-SIGNIFICANT — lowercasing would corrupt the
    address into a different (almost certainly nonexistent) key. Only hex
    (EVM) addresses have a case to normalize away."""
    return address.strip() if chain in ("tron", "bitcoin", "solana") else address.strip().lower()


def verify_evm(address: str, message: str, signature: str) -> bool:
    """Does this signature prove control of the key behind `address`?

    The signed payload is NOT the message text. Read from OKX's own verifier
    (``common/hash.go``: ``HashEvmCoinTypeMsg``), it is::

        keccak256( "\\x19Ethereum Signed Message:\\n32" || keccak256(message) )

    i.e. EIP-191 over the message's 32-byte keccak digest rather than over the
    text. Verifying against the text instead recovers a valid but unrelated
    address every time, so a naive check silently rejects every genuine entry.
    """
    try:
        digest = keccak(text=message)
        recovered = Account.recover_message(encode_defunct(primitive=digest), signature=signature)
    except Exception:
        return False
    return recovered.lower() == address.strip().lower()


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--csv", required=True, type=Path, help="published PoR file")
    parser.add_argument("--entity", required=True, help='exchange name, e.g. "OKX"')
    parser.add_argument("--source-url", required=True, help="the disclosure page")
    parser.add_argument("--out", required=True, type=Path, help="labelpack to write")
    parser.add_argument(
        "--confidence",
        type=float,
        default=0.9,
        help="confidence for a signature-verified address (default 0.9, must be < 1.0)",
    )
    args = parser.parse_args()

    if not (0.0 < args.confidence < 1.0):
        print("error: confidence must be in (0, 1) — a claim is never certainty", file=sys.stderr)
        return 2

    # These files carry a per-coin summary table BEFORE the address table, so
    # the first line is not the header. Find the row that actually declares
    # addresses, and parse from there.
    lines = args.csv.read_text().splitlines()
    start = next(
        (
            i
            for i, line in enumerate(lines)
            if "address" in line.lower() and "message" in line.lower()
        ),
        None,
    )
    if start is None:
        print(
            "error: no address/message header row found — is this a proof-of-reserves "
            "address file?",
            file=sys.stderr,
        )
        return 2
    rows = list(csv.DictReader(lines[start:]))
    verified: dict[tuple[str, str], dict[str, Any]] = {}
    outcome: Counter[str] = Counter()

    for row in rows:
        # Ragged rows (the trailing columns vary) surface as a None key whose
        # value is a list; normalise header case and spacing while dropping them.
        clean = {
            k.strip().lower(): v.strip()
            for k, v in row.items()
            if isinstance(k, str) and isinstance(v, str)
        }
        address = clean.get("address", "")
        network = clean.get("network", "").upper()
        message = clean.get("message", "")
        signature = clean.get("signature1", "") or clean.get("signature", "")
        if not address or not message:
            outcome["skipped: not an address row"] += 1
            continue
        chain = (
            EVM_NETWORKS.get(network)
            or TRON_NETWORKS.get(network)
            or BTC_NETWORKS.get(network)
            or SOL_NETWORKS.get(network)
        )
        if chain is None:
            outcome[f"dropped: {network or 'unknown'} not verifiable by this script"] += 1
            continue
        if not signature:
            outcome["dropped: no signature"] += 1
            continue
        if network in SOL_NETWORKS:
            verified_ok = verify_solana(address, message, signature)
        elif network in BTC_NETWORKS:
            verified_ok = verify_bitcoin(
                address,
                message,
                signature,
                clean.get("signature2", ""),
                clean.get("redeem script/ public key", ""),
            )
        elif network in TRON_NETWORKS:
            verified_ok = verify_tron(address, message, signature)
        else:
            verified_ok = verify_evm(address, message, signature)
        if not verified_ok:
            outcome[f"dropped: {chain} SIGNATURE DID NOT VERIFY"] += 1
            continue
        canonical = canonical_address(chain, address)
        verified[(chain, canonical)] = {
            "chain": chain,
            "address": canonical,
            "entity": args.entity,
            "category": "vasp",
            "confidence": args.confidence,
            "source_url": args.source_url,
            "verified_by": f"signature over {message!r}",
        }
        outcome["VERIFIED"] += 1

    print(f"read {len(rows)} row(s) from {args.csv}")
    for reason, count in outcome.most_common():
        print(f"  {count:6}  {reason}")

    if not verified:
        print(
            "\nnothing verified — refusing to write a pack. An unverified address "
            "must not reach a forensic report.",
            file=sys.stderr,
        )
        return 1

    pack = {
        "source": f"{args.entity} proof-of-reserves, signature-verified",
        "source_date": datetime.now(tz=UTC).date().isoformat(),
        "method": "signature",
        "license": "see source_url",
        "_note": (
            f"Each address below is accompanied in {args.entity}'s published file by a "
            "signature over a fixed message; this pack contains ONLY those whose signature "
            "recovered to the claimed address. Reserve wallets may differ from "
            "deposit-collection and withdrawal hot wallets."
        ),
        "default_confidence": args.confidence,
        "labels": sorted(verified.values(), key=lambda entry: (entry["chain"], entry["address"])),
    }
    args.out.write_text(json.dumps(pack, indent=1) + "\n")
    print(f"\nwrote {len(verified)} verified address(es) to {args.out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
