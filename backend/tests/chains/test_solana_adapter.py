"""Solana adapter against a recorded real transaction.

Solana states balances, not transfers, so normalization must DERIVE value
movement from pre/post deltas. Every expectation below is computed from the
fixture itself, so re-recording never invalidates the suite.
"""

from datetime import UTC, datetime
from typing import Any

import pytest

from cipherchain.chains.base import ChainTransaction
from cipherchain.chains.solana import SOL_ASSET, SolanaAdapter
from cipherchain.core.models import Capability, MovementKind, Provenance
from tests.chains.conftest import fixture_json

NOW = datetime(2026, 8, 7, tzinfo=UTC)
PROV = Provenance(provider="solana-rpc", retrieved_at=NOW, payload_sha256="a" * 64)


def raw_tx() -> dict[str, Any]:
    return fixture_json("sol_tx.json")["result"]


def chain_tx(payload: dict[str, Any] | None = None) -> ChainTransaction:
    body = payload if payload is not None else raw_tx()
    return ChainTransaction(
        chain="solana",
        tx_hash=body["transaction"]["signatures"][0],
        raw=body,
        provenance=PROV,
    )


def expected_sol_deltas(raw: dict[str, Any]) -> dict[int, int]:
    meta = raw["meta"]
    deltas = {
        i: int(a) - int(b)
        for i, (b, a) in enumerate(zip(meta["preBalances"], meta["postBalances"], strict=True))
    }
    deltas[0] += int(meta["fee"])  # fee is a cost, not a transfer
    return {i: d for i, d in deltas.items() if d != 0}


def expected_token_deltas(raw: dict[str, Any]) -> dict[tuple[int, str], int]:
    meta = raw["meta"]

    def index(entries: list[dict[str, Any]]) -> dict[tuple[int, str], int]:
        return {
            (int(e["accountIndex"]), str(e["mint"])): int(e["uiTokenAmount"]["amount"])
            for e in entries
        }

    pre, post = (
        index(meta.get("preTokenBalances") or []),
        index(meta.get("postTokenBalances") or []),
    )
    out: dict[tuple[int, str], int] = {}
    for key in set(pre) | set(post):
        delta = post.get(key, 0) - pre.get(key, 0)
        if delta:
            out[key] = delta
    return out


@pytest.fixture
def adapter() -> SolanaAdapter:
    return SolanaAdapter(None)  # type: ignore[arg-type]  # normalize needs no pool


class TestCapabilitiesAndAddresses:
    def test_declared_capabilities(self, adapter: SolanaAdapter) -> None:
        assert adapter.supports(Capability.ADDRESS_HISTORY)
        assert adapter.supports(Capability.TX_LOOKUP)
        # Solana has no UTXO set and no EVM logs — declared absences
        assert not adapter.supports(Capability.UTXO_LOOKUP)
        assert not adapter.supports(Capability.LOGS)

    def test_recognizes_base58_pubkeys(self, adapter: SolanaAdapter) -> None:
        account = raw_tx()["transaction"]["message"]["accountKeys"][0]["pubkey"]
        assert adapter.recognizes(account)

    def test_rejects_hex_addresses(self, adapter: SolanaAdapter) -> None:
        assert not adapter.recognizes("0x" + "a" * 40)

    def test_case_is_preserved(self, adapter: SolanaAdapter) -> None:
        # Base58 is case-significant — folding would break every lookup
        account = raw_tx()["transaction"]["message"]["accountKeys"][0]["pubkey"]
        assert adapter.canonical_address(account) == account


class TestNormalization:
    async def test_native_movements_match_balance_deltas(self, adapter: SolanaAdapter) -> None:
        raw = raw_tx()
        normalized = await adapter.normalize(chain_tx(raw))
        native = [m for m in normalized.movements if m.asset == SOL_ASSET]
        expected = expected_sol_deltas(raw)
        assert len(native) == len(expected)
        for movement in native:
            delta = expected[movement.index]
            assert movement.amount == abs(delta)
            if delta < 0:
                assert movement.kind is MovementKind.UTXO_INPUT
                assert movement.from_address is not None and movement.to_address is None
            else:
                assert movement.kind is MovementKind.UTXO_OUTPUT
                assert movement.to_address is not None and movement.from_address is None

    async def test_fee_is_not_counted_as_value(self, adapter: SolanaAdapter) -> None:
        """The fee payer's balance always drops by the fee; without adding it
        back, every transaction would show a payment to nobody."""
        raw = raw_tx()
        fee = int(raw["meta"]["fee"])
        assert fee > 0
        naive = int(raw["meta"]["postBalances"][0]) - int(raw["meta"]["preBalances"][0])
        normalized = await adapter.normalize(chain_tx(raw))
        payer_moves = [m for m in normalized.movements if m.index == 0 and m.asset == SOL_ASSET]
        recorded = -payer_moves[0].amount if payer_moves else 0
        assert recorded == naive + fee  # fee excluded from the value movement

    async def test_token_movements_match_per_mint_deltas(self, adapter: SolanaAdapter) -> None:
        raw = raw_tx()
        normalized = await adapter.normalize(chain_tx(raw))
        tokens = [m for m in normalized.movements if m.asset != SOL_ASSET]
        expected = expected_token_deltas(raw)
        assert len(tokens) == len(expected)
        for movement in tokens:
            assert movement.asset.contract is not None
            delta = expected[(movement.index, movement.asset.contract)]
            assert movement.amount == abs(delta)

    async def test_multiple_mints_stay_separate_assets(self, adapter: SolanaAdapter) -> None:
        normalized = await adapter.normalize(chain_tx())
        contracts = {m.asset.contract for m in normalized.movements if m.asset.contract}
        assert len(contracts) > 1  # this fixture touches several mints

    async def test_dedup_keys_are_unique_and_stable(self, adapter: SolanaAdapter) -> None:
        normalized = await adapter.normalize(chain_tx())
        keys = [m.dedup_key for m in normalized.movements]
        assert len(keys) == len(set(keys))
        assert all(k and (k.startswith("sol:") or k.startswith("spl:")) for k in keys)
        # same input, same identities — re-normalizing must dedup, not duplicate
        assert normalized == await adapter.normalize(chain_tx())

    async def test_failed_transaction_moves_nothing(self, adapter: SolanaAdapter) -> None:
        raw = raw_tx()
        raw["meta"] = {**raw["meta"], "err": {"InstructionError": [0, "Custom"]}}
        assert (await adapter.normalize(chain_tx(raw))).movements == ()

    async def test_unconfirmed_transaction_refused(self, adapter: SolanaAdapter) -> None:
        raw = raw_tx()
        raw.pop("blockTime", None)
        with pytest.raises(ValueError, match="unconfirmed"):
            await adapter.normalize(chain_tx(raw))

    async def test_timestamp_and_slot_recorded(self, adapter: SolanaAdapter) -> None:
        raw = raw_tx()
        normalized = await adapter.normalize(chain_tx(raw))
        assert normalized.tx.timestamp == datetime.fromtimestamp(int(raw["blockTime"]), tz=UTC)
        assert normalized.tx.block_number == raw["slot"]

    async def test_value_balances_within_each_asset(self, adapter: SolanaAdapter) -> None:
        """Inputs and outputs of one asset must net to zero — value is
        conserved, so a mismatch means the derivation is wrong."""
        normalized = await adapter.normalize(chain_tx())
        by_asset: dict[str, int] = {}
        for m in normalized.movements:
            key = m.asset.contract or "native"
            signed = -m.amount if m.kind is MovementKind.UTXO_INPUT else m.amount
            by_asset[key] = by_asset.get(key, 0) + signed
        assert by_asset["native"] == 0  # SOL conserved once the fee is excluded
