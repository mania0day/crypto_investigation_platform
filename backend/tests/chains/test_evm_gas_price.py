"""Gas price on EVM movements — the field the unique-gas-price mixer rung reads.

Rung 3 of the mixer exit ladder links a deposit to a withdrawal by EXACT
equality of a hand-set gas price (``analysis/mixers/exits.py``). The column
existed in the fact store and the domain model did not, so the adapter
populated nothing: on live data the rung could only ever fire on rows some
backfill had touched, and nothing backfills. These tests lock the acquisition
half of that path — the half where the number is actually read.

Two rules the rung depends on and every test below is about:

- the price must be the one PAID, identically from either dialect, because
  equality is exact and a cap bid is a different number;
- absence must stay ``None``. A movement with no gas price rendered as ``0``
  matches every other such movement exactly, which is how this rung would
  come to name a stranger.
"""

from datetime import UTC, datetime
from typing import Any

import pytest

from cipherchain.chains.base import ChainTransaction
from cipherchain.chains.bitcoin import BitcoinAdapter
from cipherchain.chains.evm import ETHEREUM_CONFIG, EvmAdapter
from cipherchain.chains.evm.adapter import _gas_price
from cipherchain.core.models import (
    Address,
    Asset,
    AssetKind,
    Movement,
    MovementKind,
    Provenance,
    TxRef,
)
from tests.chains.conftest import MIXER_FUNDED_ADDRESS, fixture_json

NOW = datetime(2026, 8, 16, tzinfo=UTC)
PROV = Provenance(provider="test", retrieved_at=NOW, payload_sha256="a" * 64)


def rpc_transaction(
    *,
    tx_updates: dict[str, Any] | None = None,
    receipt_updates: dict[str, Any] | None = None,
    drop_from_tx: tuple[str, ...] = (),
    drop_from_receipt: tuple[str, ...] = (),
) -> ChainTransaction:
    """The recorded RPC payloads, optionally amended around the fee fields.

    Built from the real fixture rather than from scratch so a re-recording that
    changes the shape of a transaction reaches these tests too.
    """
    tx_obj: dict[str, Any] = dict(fixture_json("eth_rpc_tx.json")["result"])
    receipt: dict[str, Any] = dict(fixture_json("eth_rpc_receipt.json")["result"])
    for field in drop_from_tx:
        tx_obj.pop(field, None)
    for field in drop_from_receipt:
        receipt.pop(field, None)
    tx_obj.update(tx_updates or {})
    receipt.update(receipt_updates or {})
    return ChainTransaction(
        chain="ethereum",
        tx_hash=str(tx_obj["hash"]).lower(),
        raw={
            "source": "rpc",
            "tx": tx_obj,
            "receipt": receipt,
            "timestamp": int(NOW.timestamp()),
            "prov_tx": PROV,
            "prov_receipt": PROV,
        },
        provenance=PROV,
    )


def etherscan_transaction(
    *,
    txlist_row: dict[str, Any] | None = None,
    token_rows: list[dict[str, Any]] | None = None,
    internal_rows: list[dict[str, Any]] | None = None,
) -> ChainTransaction:
    base = txlist_row or (token_rows or internal_rows or [{}])[0]
    return ChainTransaction(
        chain="ethereum",
        tx_hash=str(base.get("hash", "0xtx")).lower(),
        raw={
            "source": "etherscan",
            "txlist_row": txlist_row,
            "token_rows": token_rows or [],
            "internal_rows": internal_rows or [],
            "prov_txlist": PROV,
            "prov_token": PROV,
            "prov_internal": PROV,
        },
        provenance=PROV,
    )


def native_row(**overrides: Any) -> dict[str, Any]:
    row = {
        "hash": "0xtx",
        "blockNumber": "100",
        "timeStamp": str(int(NOW.timestamp())),
        "from": "0x" + "a" * 40,
        "to": "0x" + "b" * 40,
        "value": "1000",
        "isError": "0",
        "gasPrice": "31337000000",
    }
    row.update(overrides)
    return row


def internal_row(**overrides: Any) -> dict[str, Any]:
    row = {
        "hash": "0xtx",
        "blockNumber": "100",
        "timeStamp": str(int(NOW.timestamp())),
        "from": "0x" + "c" * 40,
        "to": "0x" + "d" * 40,
        "value": "500",
        "isError": "0",
        "traceId": "0_1",
    }
    row.update(overrides)
    return row


def adapter() -> EvmAdapter:
    """Normalization never touches the pool, so no provider is needed here."""
    return EvmAdapter(ETHEREUM_CONFIG, object())  # type: ignore[arg-type]


# ── which field the price is read from ───────────────────────────────────


async def test_an_eip1559_transaction_keys_on_the_price_paid_not_the_cap_bid() -> None:
    """The regression this whole module exists for, in one assertion.

    A type-2 transaction names a cap (``maxFeePerGas``) and a tip, and pays
    base fee + tip — reported as the receipt's ``effectiveGasPrice``. Etherscan
    reports that same paid number in ``gasPrice``, so keying on the cap would
    make the two vantages disagree about one transaction and the exact-equality
    rung would stop matching without anything raising.
    """
    tx = rpc_transaction(
        tx_updates={
            "type": "0x2",
            "maxFeePerGas": hex(99_000_000_000),
            "maxPriorityFeePerGas": hex(2_000_000_000),
            "gasPrice": hex(77_000_000_000),
        },
        receipt_updates={"effectiveGasPrice": hex(31_337_000_000)},
    )
    normalized = await adapter().normalize(tx)

    assert normalized.movements
    assert {m.gas_price for m in normalized.movements} == {31_337_000_000}


async def test_a_legacy_transaction_falls_back_to_the_transactions_gas_price() -> None:
    """Pre-London transactions carry no cap fields, and pre-Byzantium receipts
    predate ``effectiveGasPrice`` — the fallback is the only reading there is,
    and it is exactly the hand-set price this rung was designed around."""
    tx = rpc_transaction(
        tx_updates={"gasPrice": hex(20_000_000_001)},
        drop_from_tx=("type", "maxFeePerGas", "maxPriorityFeePerGas"),
        drop_from_receipt=("effectiveGasPrice",),
    )
    normalized = await adapter().normalize(tx)

    assert normalized.movements
    assert {m.gas_price for m in normalized.movements} == {20_000_000_001}


async def test_the_recorded_transaction_carries_its_effective_gas_price(
    fixture_pool, manifest
) -> None:
    """End to end through the real acquisition path, asserted against the
    fixture's own bytes."""
    evm = EvmAdapter(ETHEREUM_CONFIG, fixture_pool)
    normalized = await evm.normalize(await evm.transaction(manifest["eth_token_tx"]))
    receipt = fixture_json("eth_rpc_receipt.json")["result"]

    expected = int(str(receipt["effectiveGasPrice"]), 16)
    assert normalized.movements
    assert {m.gas_price for m in normalized.movements} == {expected}


async def test_both_encodings_of_one_price_parse_to_the_same_integer() -> None:
    """RPC quotes hex, Etherscan quotes decimal. The rung compares for exact
    equality across whatever produced each side, so one number written two ways
    must land as one integer — otherwise a deposit read from one vantage can
    never match a withdrawal read from the other."""
    price = 31_337_000_000
    rpc = await adapter().normalize(
        rpc_transaction(receipt_updates={"effectiveGasPrice": hex(price)})
    )
    rows = await adapter().normalize(
        etherscan_transaction(txlist_row=native_row(gasPrice=str(price)))
    )

    assert {m.gas_price for m in rpc.movements} == {m.gas_price for m in rows.movements} == {price}


# ── inheritance: a price belongs to the transaction, not the transfer ─────


async def test_a_token_transfer_inherits_the_gas_price_of_its_transaction(
    fixture_pool, manifest
) -> None:
    """A Transfer log names no gas price; the transaction that emitted it does.

    Reading the price per-log would leave every token movement null, and token
    movements are the common case the ladder walks (stablecoins), so the rung
    would have been dead for the traffic that matters most.
    """
    evm = EvmAdapter(ETHEREUM_CONFIG, fixture_pool)
    normalized = await evm.normalize(await evm.transaction(manifest["eth_token_tx"]))
    expected = int(str(fixture_json("eth_rpc_receipt.json")["result"]["effectiveGasPrice"]), 16)

    tokens = [m for m in normalized.movements if m.kind is MovementKind.TOKEN]
    assert tokens, "the recorded receipt contains Transfer logs"
    assert all(m.gas_price == expected for m in tokens)


async def test_an_etherscan_token_row_and_native_row_share_one_price() -> None:
    """Both feeds repeat the transaction's price; a token row that omits it
    still inherits the transaction's, because it was the same payment."""
    token = {
        **native_row(),
        "contractAddress": "0x" + "e" * 40,
        "tokenSymbol": "USDT",
        "tokenDecimal": "6",
        "value": "250000",
    }
    del token["gasPrice"]  # the row omits it; the transaction still has one
    normalized = await adapter().normalize(
        etherscan_transaction(txlist_row=native_row(), token_rows=[token])
    )

    kinds = {m.kind for m in normalized.movements}
    assert kinds == {MovementKind.NATIVE, MovementKind.TOKEN}
    assert {m.gas_price for m in normalized.movements} == {31_337_000_000}


async def test_an_internal_transfer_inherits_the_price_of_the_paying_transaction() -> None:
    """``txlistinternal`` rows carry no gas price at all — an internal call is
    a call inside a transaction someone else paid for. Contract-delivered value
    is exactly how a mixer pays a withdrawal out, so leaving these null would
    strand the rung on the one movement shape it was written to serve."""
    normalized = await adapter().normalize(
        etherscan_transaction(txlist_row=native_row(), internal_rows=[internal_row()])
    )

    internal = [m for m in normalized.movements if m.kind is MovementKind.INTERNAL]
    assert len(internal) == 1
    assert internal[0].gas_price == 31_337_000_000


async def test_the_mixer_funded_wallets_internal_movements_carry_a_price(fixture_pool) -> None:
    """The recorded Tornado-funded wallet, through the real merge path: every
    internal movement must carry the gas price its own ``txlist`` row reports."""
    evm = EvmAdapter(ETHEREUM_CONFIG, fixture_pool)
    page = await evm.address_history(Address("ethereum", MIXER_FUNDED_ADDRESS), limit=20)
    by_hash = {
        str(row["hash"]).lower(): row
        for row in fixture_json("eth_internal_mixer_txlist.json")["result"]
    }

    checked = 0
    for item in page.items:
        for movement in (await evm.normalize(item)).movements:
            if movement.kind is not MovementKind.INTERNAL:
                continue
            assert movement.gas_price == int(by_hash[item.tx_hash]["gasPrice"])
            checked += 1
    assert checked > 0, "the fixture contains internal movements"


# ── absence stays absence ────────────────────────────────────────────────


async def test_an_absent_rpc_gas_price_stays_none_and_never_becomes_zero() -> None:
    """``None`` means "not read"; ``0`` means "paid nothing". Collapsing them
    would let two transactions with no fee data match each other exactly."""
    tx = rpc_transaction(
        drop_from_tx=("gasPrice", "maxFeePerGas", "maxPriorityFeePerGas"),
        drop_from_receipt=("effectiveGasPrice",),
    )
    normalized = await adapter().normalize(tx)

    assert normalized.movements
    for movement in normalized.movements:
        assert movement.gas_price is None
        assert movement.gas_price != 0


async def test_an_etherscan_group_with_no_price_anywhere_stays_none() -> None:
    """An internal-only transaction has no ``txlist`` row to inherit from, and
    no feed reporting a price. Unknown is the honest answer."""
    normalized = await adapter().normalize(
        etherscan_transaction(internal_rows=[internal_row()])
    )

    assert len(normalized.movements) == 1
    assert normalized.movements[0].gas_price is None


async def test_a_gas_price_of_zero_is_kept_as_zero() -> None:
    """Zero is a real, stated price and is preserved as one — it is the
    fabricated zero that is forbidden, not the reported one. The rung declines
    to match on it separately, which is where that judgement belongs."""
    normalized = await adapter().normalize(
        rpc_transaction(receipt_updates={"effectiveGasPrice": "0x0"})
    )

    assert normalized.movements
    for movement in normalized.movements:
        assert movement.gas_price == 0
        assert movement.gas_price is not None


async def test_bitcoin_movements_carry_no_gas_price(fixture_pool, manifest) -> None:
    """A chain with no notion of gas price reports none, rather than zero.

    Bitcoin prices a whole transaction, not a unit of computation. If its
    movements arrived as ``0`` they would all match each other exactly, and the
    rung's uniqueness check is per-mixer, not per-chain.
    """
    btc = BitcoinAdapter(fixture_pool)
    normalized = await btc.normalize(await btc.transaction(manifest["btc_txid"]))

    assert normalized.movements
    assert all(m.gas_price is None for m in normalized.movements)


# ── parsing: unknown rather than wrong ───────────────────────────────────


class TestGasPriceParsing:
    def test_both_dialect_encodings_are_accepted(self) -> None:
        assert _gas_price("0x4a817c800") == 20_000_000_000
        assert _gas_price("20000000000") == 20_000_000_000
        assert _gas_price("0X4A817C800") == 20_000_000_000  # non-canonical casing
        assert _gas_price(20_000_000_000) == 20_000_000_000

    def test_a_uint256_price_survives_intact(self) -> None:
        """Gas price is a uint256 on chain and the store holds NUMERIC(78,0);
        rounding one to fit would silently break exact-equality matching."""
        huge = 2**256 - 1
        assert _gas_price(hex(huge)) == huge

    def test_absent_and_empty_values_are_unknown(self) -> None:
        assert _gas_price(None) is None
        assert _gas_price("") is None
        assert _gas_price("   ") is None

    def test_unparseable_values_are_unknown_rather_than_guessed(self) -> None:
        assert _gas_price("0xnothex") is None
        assert _gas_price("twenty gwei") is None

    def test_a_negative_price_is_refused_outright(self) -> None:
        """No chain has one, so it is a parse bug. Stored, it would fail the
        ``gas_price >= 0`` CHECK mid-batch; worse, two re-parses agreeing on the
        same wrong number would read as a match between unrelated transactions.
        """
        assert _gas_price("-5") is None
        assert _gas_price("-0x5") is None


def test_a_movement_refuses_a_negative_gas_price() -> None:
    """Caught at the model boundary so the movement that is wrong is the one
    that raises — not an insert batch failing the CHECK constraint later, far
    from the adapter that produced the bad parse."""
    with pytest.raises(ValueError, match="gas_price"):
        Movement(
            tx=TxRef(chain="ethereum", tx_hash="0x1", timestamp=NOW),
            asset=Asset(chain="ethereum", kind=AssetKind.NATIVE, symbol="ETH", decimals=18),
            amount=1,
            kind=MovementKind.NATIVE,
            from_address=Address("ethereum", "0xa"),
            to_address=Address("ethereum", "0xb"),
            index=0,
            provenance=PROV,
            gas_price=-1,
        )


def test_a_movement_without_a_gas_price_defaults_to_unknown() -> None:
    """Every adapter that predates this field, and every chain without the
    concept, keeps producing movements — and none of them invent a zero."""
    movement = Movement(
        tx=TxRef(chain="ethereum", tx_hash="0x1", timestamp=NOW),
        asset=Asset(chain="ethereum", kind=AssetKind.NATIVE, symbol="ETH", decimals=18),
        amount=1,
        kind=MovementKind.NATIVE,
        from_address=Address("ethereum", "0xa"),
        to_address=Address("ethereum", "0xb"),
        index=0,
        provenance=PROV,
    )
    assert movement.gas_price is None
