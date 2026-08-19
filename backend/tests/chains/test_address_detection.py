"""Chain detection from address format.

Address format is per-chain knowledge, declared by each adapter and resolved
by the registry. Getting this wrong means tracing the wrong ledger and
answering confidently about the wrong money, so ambiguity must surface
rather than be guessed.
"""

import pytest

from cipherchain.chains.base import ChainRegistry
from cipherchain.chains.bitcoin import BitcoinAdapter
from cipherchain.chains.evm import ETHEREUM_CONFIG, EvmAdapter, EvmChainConfig

BTC_P2PKH = "1A1zP1eP5QGefi2DMPTfTL5SLmv7DivfNa"
BTC_P2SH = "3PeVz6zCzRWsRq9YfZYbfbP92ZYDNyMUCC"
BTC_BECH32 = "bc1qar0srrr7xfkvy5l643lydnw9re59gtzzwf5mdq"
ETH = "0x6bd0b42faf093541b31f94a041774d5eb30906ad"
ETH_CHECKSUM = "0x6BD0b42fAF093541b31f94A041774d5EB30906AD"


@pytest.fixture
def registry() -> ChainRegistry:
    reg = ChainRegistry()
    reg.register(BitcoinAdapter(None))  # type: ignore[arg-type]  # detection needs no pool
    reg.register(EvmAdapter(ETHEREUM_CONFIG, None))  # type: ignore[arg-type]
    return reg


@pytest.mark.parametrize("address", [BTC_P2PKH, BTC_P2SH, BTC_BECH32])
def test_bitcoin_formats_detected(registry: ChainRegistry, address: str) -> None:
    assert registry.detect(address) == ("bitcoin",)


@pytest.mark.parametrize("address", [ETH, ETH_CHECKSUM])
def test_ethereum_formats_detected(registry: ChainRegistry, address: str) -> None:
    assert registry.detect(address) == ("ethereum",)


def test_surrounding_whitespace_tolerated(registry: ChainRegistry) -> None:
    assert registry.detect("  " + ETH + "  ") == ("ethereum",)


@pytest.mark.parametrize(
    "address",
    [
        "",
        "not-an-address",
        "0x1234",  # too short for EVM
        "0x" + "g" * 40,  # non-hex
        ETH + "00",  # too long
        "0OIl" + "1" * 30,  # base58 excludes 0, O, I, l
    ],
)
def test_unrecognized_formats_return_nothing(registry: ChainRegistry, address: str) -> None:
    assert registry.detect(address) == ()


def test_shared_evm_format_is_reported_as_ambiguous() -> None:
    """Two EVM chains share one address format — the registry must report
    both rather than silently picking a ledger."""
    reg = ChainRegistry()
    reg.register(EvmAdapter(ETHEREUM_CONFIG, None))  # type: ignore[arg-type]
    reg.register(
        EvmAdapter(
            EvmChainConfig(chain="polygon", etherscan_chain_id=137, native_symbol="POL"),
            None,  # type: ignore[arg-type]
        )
    )
    assert reg.detect(ETH) == ("ethereum", "polygon")


class TestCanonicalAddress:
    def test_evm_lowercases(self) -> None:
        adapter = EvmAdapter(ETHEREUM_CONFIG, None)  # type: ignore[arg-type]
        assert adapter.canonical_address(ETH_CHECKSUM) == ETH

    def test_base58_case_is_preserved(self) -> None:
        # Base58 is case-SIGNIFICANT: folding it would break every lookup.
        adapter = BitcoinAdapter(None)  # type: ignore[arg-type]
        assert adapter.canonical_address(BTC_P2SH) == BTC_P2SH

    def test_bech32_lowercased(self) -> None:
        adapter = BitcoinAdapter(None)  # type: ignore[arg-type]
        assert adapter.canonical_address(BTC_BECH32.upper()) == BTC_BECH32
