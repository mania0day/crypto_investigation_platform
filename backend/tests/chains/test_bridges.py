"""Bridge registry and hint emission.

A bridge hint claims funds left the chain. Getting it wrong sends an
investigator to the wrong ledger, so the pack format demands provenance and
the adapter emits a hint only for an address the operator registered.
"""

import json
from datetime import UTC, datetime
from pathlib import Path

import pytest

from cipherchain.chains.base import BridgeDirection, ChainTransaction
from cipherchain.chains.bridges import (
    BridgeEntry,
    BridgeRegistry,
    build_bridge_registry,
    load_bridge_pack,
)
from cipherchain.chains.evm import ETHEREUM_CONFIG, EvmAdapter
from cipherchain.core.errors import ConfigurationError
from cipherchain.core.models import Provenance

NOW = datetime(2026, 8, 7, tzinfo=UTC)
PROV = Provenance(provider="etherscan", retrieved_at=NOW, payload_sha256="a" * 64)
BRIDGE_ADDR = "0xAAAA000000000000000000000000000000001111"
WALLET = "0xbbbb000000000000000000000000000000002222"


def entry(address: str = BRIDGE_ADDR, chain: str = "ethereum") -> BridgeEntry:
    return BridgeEntry(
        bridge_id="polygon-pos",
        name="Test Bridge",
        chain=chain,
        address=address,
        direction=BridgeDirection.DEPOSIT,
        counterpart_chain="polygon",
        source="test-pack",
        source_date=NOW,
    )


class TestRegistry:
    def test_hex_lookup_is_case_insensitive(self) -> None:
        registry = BridgeRegistry([entry()])
        assert registry.lookup("ethereum", BRIDGE_ADDR.lower()) is not None
        assert registry.lookup("ethereum", BRIDGE_ADDR.upper()) is not None

    def test_lookup_is_chain_scoped(self) -> None:
        """The same address on another chain is a different contract."""
        registry = BridgeRegistry([entry()])
        assert registry.lookup("polygon", BRIDGE_ADDR) is None

    def test_for_chain_view(self) -> None:
        registry = BridgeRegistry([entry(), entry(address="0xcccc", chain="polygon")])
        assert len(registry.for_chain("ethereum")) == 1
        assert registry.chains() == ("ethereum", "polygon")

    def test_provenance_is_mandatory(self) -> None:
        with pytest.raises(ValueError, match="source"):
            BridgeEntry(
                bridge_id="b",
                name="n",
                chain="ethereum",
                address="0x1",
                direction=BridgeDirection.DEPOSIT,
                counterpart_chain=None,
                source="",
            )


class TestPackLoading:
    def write(self, tmp_path: Path, payload: dict) -> Path:
        p = tmp_path / "pack.json"
        p.write_text(json.dumps(payload))
        return p

    def test_loads_valid_pack(self, tmp_path: Path) -> None:
        path = self.write(
            tmp_path,
            {
                "source": "official-docs",
                "source_date": "2026-08-07",
                "bridges": [
                    {
                        "bridge_id": "polygon-pos",
                        "name": "Polygon PoS",
                        "chain": "ethereum",
                        "address": BRIDGE_ADDR,
                        "direction": "deposit",
                        "counterpart_chain": "polygon",
                    }
                ],
            },
        )
        entries = load_bridge_pack(path)
        assert len(entries) == 1
        assert entries[0].counterpart_chain == "polygon"
        assert entries[0].source_date is not None

    def test_source_required(self, tmp_path: Path) -> None:
        path = self.write(tmp_path, {"bridges": []})
        with pytest.raises(ConfigurationError, match="provenance"):
            load_bridge_pack(path)

    def test_bad_entry_names_its_index(self, tmp_path: Path) -> None:
        path = self.write(tmp_path, {"source": "s", "bridges": [{"bridge_id": "b"}]})
        with pytest.raises(ConfigurationError, match="entry 0"):
            load_bridge_pack(path)

    def test_registry_is_empty_when_no_packs_supplied(self, tmp_path: Path) -> None:
        """CipherChain ships no bridge addresses — absent a pack, no bridge
        findings can be produced."""
        assert len(build_bridge_registry(tmp_path)) == 0
        assert len(build_bridge_registry(None)) == 0


class TestHintEmission:
    def etherscan_tx(self, to_address: str, from_address: str = WALLET) -> ChainTransaction:
        row = {
            "hash": "0xfeed",
            "from": from_address,
            "to": to_address,
            "value": "1000000000000000000",
            "timeStamp": "1786000000",
            "blockNumber": "100",
            "isError": "0",
        }
        return ChainTransaction(
            chain="ethereum",
            tx_hash="0xfeed",
            raw={
                "source": "etherscan",
                "txlist_row": row,
                "token_rows": [],
                "prov_txlist": PROV,
                "prov_token": PROV,
            },
            provenance=PROV,
        )

    async def test_no_registry_means_no_hints(self) -> None:
        adapter = EvmAdapter(ETHEREUM_CONFIG, None)  # type: ignore[arg-type]
        normalized = await adapter.normalize(self.etherscan_tx(BRIDGE_ADDR))
        assert normalized.bridge_hints == ()

    async def test_deposit_into_registered_bridge_emits_hint(self) -> None:
        adapter = EvmAdapter(
            ETHEREUM_CONFIG,
            None,  # type: ignore[arg-type]
            bridges=BridgeRegistry([entry()]),
        )
        normalized = await adapter.normalize(self.etherscan_tx(BRIDGE_ADDR))
        assert len(normalized.bridge_hints) == 1
        hint = normalized.bridge_hints[0]
        assert hint.bridge_id == "polygon-pos"
        assert hint.direction is BridgeDirection.DEPOSIT
        assert hint.counterpart_chain == "polygon"
        assert normalized.tx.tx_hash in hint.refs

    async def test_value_arriving_from_bridge_is_a_withdrawal(self) -> None:
        adapter = EvmAdapter(
            ETHEREUM_CONFIG,
            None,  # type: ignore[arg-type]
            bridges=BridgeRegistry([entry()]),
        )
        normalized = await adapter.normalize(self.etherscan_tx(WALLET, from_address=BRIDGE_ADDR))
        assert len(normalized.bridge_hints) == 1
        assert normalized.bridge_hints[0].direction is BridgeDirection.WITHDRAWAL

    async def test_ordinary_transfer_emits_no_hint(self) -> None:
        adapter = EvmAdapter(
            ETHEREUM_CONFIG,
            None,  # type: ignore[arg-type]
            bridges=BridgeRegistry([entry()]),
        )
        normalized = await adapter.normalize(self.etherscan_tx("0xdddd" + "0" * 36))
        assert normalized.bridge_hints == ()

    async def test_hints_are_deterministic(self) -> None:
        adapter = EvmAdapter(
            ETHEREUM_CONFIG,
            None,  # type: ignore[arg-type]
            bridges=BridgeRegistry([entry()]),
        )
        tx = self.etherscan_tx(BRIDGE_ADDR)
        assert (await adapter.normalize(tx)) == (await adapter.normalize(tx))
