"""The proof-of-reserves importer must verify, not rubber-stamp.

An exchange address that reaches a forensic report on the strength of "it was
in a file we downloaded" is worth very little. These tests pin the property
that matters: a signature is only accepted when it actually recovers to the
claimed address.

The signed payload is NOT the message text. From OKX's own verifier
(`common/hash.go`, `HashEvmCoinTypeMsg`)::

    keccak256( "\\x19Ethereum Signed Message:\\n32" || keccak256(message) )

Getting this wrong is not loud — verifying against the text recovers a valid
but unrelated address for every row, so a naive implementation rejects 100% of
genuine entries and looks like it is working strictly. That failure mode is why
`test_published_rows_verify` exists: without a known-good positive fixture, an
all-negative suite passes while the tool accepts nothing.
"""

import importlib.util
import json
from pathlib import Path

import pytest

pytest.importorskip("eth_account", reason="signature verification is a scripts-extra dependency")

SCRIPT = Path(__file__).resolve().parents[2] / "scripts" / "import_por_labelpack.py"
MESSAGE = "I am an OKX address"

# Real rows from OKX's published example file. These are GENUINE — they recover
# correctly under the real scheme — so they are the known-good fixture that
# proves the verifier accepts what it should.
PUBLISHED_ROWS = [
    (
        "0x0cdcdb19a857c2ac24818ca4fdfe38cce071483e",
        "0x07f19879aa28d51c97cddfdfecffe7ed96525545d041aee4f4386b0bf4c1a269"
        "24b637fb02ccbb97305c13daa51a0f50b8896fb25ecbaf60020cde920d227a221b",
    ),
    (
        "0x16f01cfc16b0b8c3400fb8e5099b0974fcc9fc12",
        "0xf6e60fd5d2692eaf813cbb38369c160781a063e39dbadde9a88998a4c8019e08"
        "488f83a6e7ee5e5b3fbdd50f25db631150070e8bb470d1474eb0e6d9a1fd0c561c",
    ),
]

HEADER = "coin,Network,Snapshot Height,address,amount,message,signature1,signature2\n"


def load_script():
    spec = importlib.util.spec_from_file_location("por_import", SCRIPT)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def okx_sign(account) -> str:
    """Sign the way OKX does: EIP-191 over keccak(message), not over the text."""
    from eth_account.messages import encode_defunct
    from eth_utils import keccak

    return account.sign_message(encode_defunct(primitive=keccak(text=MESSAGE))).signature.hex()


def test_published_rows_verify() -> None:
    """The positive control, against real published data.

    If this fails, the verifier is rejecting genuine entries — the silent
    failure that produced an empty pack from a file of 15,000 valid addresses.
    """
    module = load_script()
    for address, signature in PUBLISHED_ROWS:
        assert module.verify_evm(address, MESSAGE, signature), (
            f"{address} is a genuine published OKX address and must verify"
        )


def test_a_freshly_signed_address_verifies() -> None:
    from eth_account import Account

    module = load_script()
    account = Account.create()
    assert module.verify_evm(account.address, MESSAGE, okx_sign(account))


def test_a_signature_for_a_different_address_is_rejected() -> None:
    """The attack this guards: reusing a valid signature under someone else's
    address, which is how a fake exchange label would be smuggled in."""
    from eth_account import Account

    module = load_script()
    signer, other = Account.create(), Account.create()
    signature = okx_sign(signer)
    assert module.verify_evm(signer.address, MESSAGE, signature)
    assert not module.verify_evm(other.address, MESSAGE, signature)


def test_a_signature_over_a_different_message_is_rejected() -> None:
    from eth_account import Account
    from eth_account.messages import encode_defunct
    from eth_utils import keccak

    module = load_script()
    account = Account.create()
    wrong = account.sign_message(
        encode_defunct(primitive=keccak(text="I am NOT an OKX address"))
    ).signature.hex()
    assert not module.verify_evm(account.address, MESSAGE, wrong)


def test_malformed_signature_is_rejected_not_raised() -> None:
    module = load_script()
    assert not module.verify_evm(PUBLISHED_ROWS[0][0], MESSAGE, "0xnotasignature")


def _run(module, csv_path: Path, out: Path) -> int:
    import sys

    argv = sys.argv
    sys.argv = [
        "import_por_labelpack.py",
        "--csv",
        str(csv_path),
        "--entity",
        "OKX",
        "--source-url",
        "https://www.okx.com/proof-of-reserves",
        "--out",
        str(out),
    ]
    try:
        return int(module.main())
    finally:
        sys.argv = argv


def test_only_verified_rows_reach_the_pack(tmp_path: Path) -> None:
    from eth_account import Account

    module = load_script()
    genuine, impostor = Account.create(), Account.create()
    stolen = okx_sign(genuine)  # a real signature, claimed by the wrong address

    csv_path = tmp_path / "por.csv"
    csv_path.write_text(
        "coin,snapshot height,amount\nBTC,1,1\n\n"  # the summary table that precedes it
        + HEADER
        + f"ETH,ETH,1,{genuine.address},1.0,{MESSAGE},{okx_sign(genuine)},\n"
        + f"ETH,ETH,1,{impostor.address},1.0,{MESSAGE},{stolen},\n"
    )
    out = tmp_path / "verified-vasps.json"
    assert _run(module, csv_path, out) == 0

    pack = json.loads(out.read_text())
    addresses = [entry["address"] for entry in pack["labels"]]
    assert addresses == [genuine.address.lower()], "only the verified address may ship"
    assert impostor.address.lower() not in addresses
    assert pack["source_date"]
    assert all(entry["category"] == "vasp" for entry in pack["labels"])
    assert all(0.0 < entry["confidence"] < 1.0 for entry in pack["labels"])


def test_nothing_verified_means_no_pack_is_written(tmp_path: Path) -> None:
    from eth_account import Account

    module = load_script()
    genuine, impostor = Account.create(), Account.create()
    csv_path = tmp_path / "por.csv"
    csv_path.write_text(
        HEADER + f"ETH,ETH,1,{impostor.address},1.0,{MESSAGE},{okx_sign(genuine)},\n"
    )
    out = tmp_path / "verified-vasps.json"
    assert _run(module, csv_path, out) == 1
    assert not out.exists(), "an unverified address must never reach a pack"


# ── Bitcoin: 2-of-3 multisig, where BOTH halves must hold ────────────────────

# A real published OKX Bitcoin row: 2-of-3 multisig, P2SH.
BTC_ADDRESS = "31qfemWiP2VdksYRPtcX1BDr48AfxXm44L"
BTC_SIG1 = (
    "H7dNM9UDdH4pwFht7MO0wAomEA2sG5hAH1T/yawnDn2KC1VOiPA85EL+xsT+fLxh48xlOrcSbon27ZxCKFAU+m8="
)
BTC_SIG2 = (
    "II2+x1ksyGHx5FC31L3WDgVrEYXhZj+oD00V4sX8AZ1LbUd6QbP71+0b+icLqCUily5MtFYhewfIelFm5djhI6M="
)
BTC_SCRIPT = (
    "52210310812d3e5bb4cb0645b17ada34701e905121f170a58537623012b1387f"
    "d208f82103393d7b2dbc19c0143aaa08f32d250d270a1283b77ad74cb55e58c4"
    "34e95d4cd32102a8ab89a8d158a5c9adf7684551e7a195adf87106580fb836f6"
    "84c173adeb600f53ae"
)


def test_bitcoin_multisig_row_verifies() -> None:
    """Positive control against real published data.

    Verification has two halves — the signatures must recover to pubkeys inside
    the redeem script, and the script must hash to the claimed address. A
    verifier doing only one of them looks identical until it is attacked.
    """
    module = load_script()
    assert module.verify_bitcoin(BTC_ADDRESS, MESSAGE, BTC_SIG1, BTC_SIG2, BTC_SCRIPT)


def test_bitcoin_rejects_a_script_that_is_not_this_address() -> None:
    """Signatures alone prove nothing about THIS address."""
    module = load_script()
    other = "53" + BTC_SCRIPT[2:]  # OP_2 -> OP_3 changes the script hash
    assert not module.verify_bitcoin(BTC_ADDRESS, MESSAGE, BTC_SIG1, BTC_SIG2, other)


def test_bitcoin_rejects_signatures_not_in_the_script() -> None:
    """The address link alone proves nothing about key control."""
    module = load_script()
    import base64

    # A well-formed compact signature that recovers to some other key.
    bogus = base64.b64encode(bytes([31]) + b"\x11" * 64).decode()
    assert not module.verify_bitcoin(BTC_ADDRESS, MESSAGE, bogus, BTC_SIG2, BTC_SCRIPT)


def test_bitcoin_refuses_without_a_redeem_script() -> None:
    """16 published rows carry signatures but no script — unverifiable by
    construction, and dropped rather than shipped with a caveat."""
    module = load_script()
    assert not module.verify_bitcoin(BTC_ADDRESS, MESSAGE, BTC_SIG1, BTC_SIG2, "")


# ── Solana ───────────────────────────────────────────────────────────────────
# Mutation testing found verify_solana coverable by `return True` with a green
# suite — an unproven address would ship in the signature-verified pack at 0.9.
# These tests are the teeth: real ed25519 keys, both accept and reject paths.

_B58_ALPHABET = "123456789ABCDEFGHJKLMNPQRSTUVWXYZabcdefghijkmnopqrstuvwxyz"


def _b58encode(raw: bytes) -> str:
    number = int.from_bytes(raw, "big")
    out = ""
    while number:
        number, rem = divmod(number, 58)
        out = _B58_ALPHABET[rem] + out
    return "1" * (len(raw) - len(raw.lstrip(b"\x00"))) + out


def _solana_keypair():
    ed25519 = pytest.importorskip(
        "cryptography.hazmat.primitives.asymmetric.ed25519",
        reason="ed25519 is a scripts-extra dependency",
    )
    from cryptography.hazmat.primitives import serialization

    key = ed25519.Ed25519PrivateKey.generate()
    pub = key.public_key().public_bytes(serialization.Encoding.Raw, serialization.PublicFormat.Raw)
    return key, _b58encode(pub)


def test_a_genuine_solana_signature_verifies_in_every_encoding() -> None:
    import base64 as b64

    module = load_script()
    key, address = _solana_keypair()
    raw = key.sign(MESSAGE.encode())
    for encoded in (b64.b64encode(raw).decode(), _b58encode(raw), raw.hex()):
        assert module.verify_solana(address, MESSAGE, encoded), (
            f"genuine ed25519 signature must verify (encoding: {encoded[:12]}…)"
        )


def test_a_solana_signature_from_a_different_key_is_rejected() -> None:
    import base64 as b64

    module = load_script()
    key, _ = _solana_keypair()
    _, other_address = _solana_keypair()
    raw = key.sign(MESSAGE.encode())
    assert not module.verify_solana(other_address, MESSAGE, b64.b64encode(raw).decode())


def test_a_solana_signature_over_a_different_message_is_rejected() -> None:
    import base64 as b64

    module = load_script()
    key, address = _solana_keypair()
    raw = key.sign(b"I am a different message entirely")
    assert not module.verify_solana(address, MESSAGE, b64.b64encode(raw).decode())


def test_solana_garbage_is_rejected_not_raised() -> None:
    module = load_script()
    _, address = _solana_keypair()
    assert not module.verify_solana(address, MESSAGE, "not-a-signature")
    assert not module.verify_solana("not-base58-0OIl", MESSAGE, "AA==")
    assert not module.verify_solana("abc", MESSAGE, "AA==")  # decodes too short


def test_canonical_form_preserves_base58_case_and_folds_hex() -> None:
    """Base58 is case-significant: lowercasing a Solana or Tron address
    denotes a different key. Only EVM hex has a case to normalize away."""
    module = load_script()
    _, address = _solana_keypair()
    assert module.canonical_address("solana", f"  {address} ") == address
    assert module.canonical_address("tron", "TAbCdEf") == "TAbCdEf"
    assert module.canonical_address("bitcoin", "bc1QaBc") == "bc1QaBc"
    assert module.canonical_address("ethereum", " 0xABCDef01 ") == "0xabcdef01"
