"""Losing one acquisition feed must cost that feed's rows, not the address.

The regression these lock down: ``address_history`` fans out into three
provider calls, and while all three were awaited on one chain, the loss of
any single one of them raised ``AllProvidersFailed`` and took the whole
page — and therefore the whole branch of the trace — with it. Quotas do run
out, and the requirement is that running out SLOWS a trace rather than
stopping it.

The other half of the requirement is the more dangerous one, so it is
asserted here too: a feed that goes missing must leave a durable record
naming itself. A run that silently lost ``tokentx`` would report "no named
endpoint" for an address funded entirely in USDT, and nothing downstream
could tell that from the truth.

The pool here is real. A provider is registered that serves only some of
the capabilities, which is exactly how a spent pool behaves in production —
a keyed provider out of quota, a circuit open, or the fetch tier's explorer
refusing to serve this host for the next hour — so the unserved feed fails
the way it fails on a live host, with no monkeypatching of the failure path
and no sleep in the retry loop.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

import pytest

from cipherchain.chains.base import FeedGap
from cipherchain.chains.evm import ETHEREUM_CONFIG, EvmAdapter
from cipherchain.core.errors import AllProvidersFailed
from cipherchain.core.hashing import canonical_json_bytes, sha256_hex
from cipherchain.core.models import Address, Capability, MovementKind
from cipherchain.providers.base import Provider, ProviderRequest, ProviderResponse
from cipherchain.providers.cache import InMemoryCache
from cipherchain.providers.pool import ProviderPool
from tests.chains.conftest import fixture_json

ADDRESS = Address("ethereum", "0xdcbeffbecce100cce9e4b153c4e15cb885643193")

FIXTURE_BY_CAPABILITY = {
    Capability.ADDRESS_HISTORY: "eth_txlist.json",
    Capability.TOKEN_TRANSFERS: "eth_tokentx.json",
    Capability.INTERNAL_TRACES: "eth_txlistinternal.json",
}


def rows(capability: Capability) -> list[dict[str, Any]]:
    result: list[dict[str, Any]] = fixture_json(FIXTURE_BY_CAPABILITY[capability])["result"]
    return result


class PartialProvider(Provider):
    """Serves the feeds it declares and no others.

    Declining a capability rather than erroring on it is the honest model of
    the real degradation: when the keyed provider's quota is gone, what is
    left in the pool genuinely cannot answer ``token_transfers``, so the
    pool finds no candidate and raises immediately.
    """

    name = "partial-fixture"

    def __init__(self, served: set[Capability]) -> None:
        self._served = served

    def supports(self, chain: str, capability: Capability) -> bool:
        return chain == "ethereum" and capability in self._served

    async def execute(self, request: ProviderRequest) -> ProviderResponse:
        payload = rows(request.capability)
        raw = canonical_json_bytes(payload)
        return ProviderResponse(
            provider=self.name,
            retrieved_at=datetime(2026, 8, 16, tzinfo=UTC),
            payload=payload,
            raw=raw,
            payload_sha256=sha256_hex(raw),
        )


def adapter_serving(*served: Capability) -> EvmAdapter:
    pool = ProviderPool(cache=InMemoryCache())
    pool.register(PartialProvider(set(served)), priority=10)
    return EvmAdapter(ETHEREUM_CONFIG, pool)


ALL_FEEDS = (Capability.ADDRESS_HISTORY, Capability.TOKEN_TRANSFERS, Capability.INTERNAL_TRACES)


def gap_for(gaps: tuple[FeedGap, ...], capability: Capability) -> FeedGap | None:
    return next((gap for gap in gaps if gap.capability is capability), None)


async def test_an_unavailable_token_feed_costs_its_rows_and_not_the_page() -> None:
    """The whole point: one dead feed must not end the branch."""
    adapter = adapter_serving(Capability.ADDRESS_HISTORY, Capability.INTERNAL_TRACES)

    page = await adapter.address_history(ADDRESS)

    native_hashes = {str(row["hash"]).lower() for row in rows(Capability.ADDRESS_HISTORY)}
    assert native_hashes <= {item.tx_hash for item in page.items}
    assert page.items  # the address still has a readable history
    assert all(item.raw["token_rows"] == [] for item in page.items)  # type: ignore[index]


async def test_an_unavailable_token_feed_is_recorded_as_a_gap() -> None:
    """A lost feed leaves a durable fact, not a log line nobody reads."""
    adapter = adapter_serving(Capability.ADDRESS_HISTORY, Capability.INTERNAL_TRACES)

    page = await adapter.address_history(ADDRESS)

    assert not page.complete
    gap = gap_for(page.gaps, Capability.TOKEN_TRANSFERS)
    assert gap is not None
    assert gap.chain == "ethereum"
    assert gap.code == "feed_unavailable:token_transfers"
    assert "token transfers" in gap.summary
    assert gap_for(page.gaps, Capability.INTERNAL_TRACES) is None  # that feed answered


async def test_an_unavailable_internal_feed_costs_its_rows_and_not_the_page() -> None:
    """Same degradation for the feed that carries contract-delivered value."""
    adapter = adapter_serving(Capability.ADDRESS_HISTORY, Capability.TOKEN_TRANSFERS)

    page = await adapter.address_history(ADDRESS)

    assert page.items
    assert all(item.raw["internal_rows"] == [] for item in page.items)  # type: ignore[index]
    gap = gap_for(page.gaps, Capability.INTERNAL_TRACES)
    assert gap is not None
    assert gap.code == "feed_unavailable:internal_traces"
    assert "internal transfers" in gap.summary
    assert gap_for(page.gaps, Capability.TOKEN_TRANSFERS) is None


async def test_an_unavailable_native_feed_is_still_a_hard_failure() -> None:
    """No native history means no page — inventing an empty one is a false empty.

    The engine cannot tell an empty page from "this address never
    transacted", so degrading here would publish the most dangerous output
    this tool has. It raises instead, and the branch fails loudly.
    """
    adapter = adapter_serving(Capability.TOKEN_TRANSFERS, Capability.INTERNAL_TRACES)

    with pytest.raises(AllProvidersFailed):
        await adapter.address_history(ADDRESS)


async def test_every_feed_answering_records_nothing() -> None:
    """No false alarms: a healthy page must not carry a coverage caveat."""
    adapter = adapter_serving(*ALL_FEEDS)

    page = await adapter.address_history(ADDRESS)

    assert page.gaps == ()
    assert page.complete
    assert page.items


async def test_each_lost_feed_is_named_separately() -> None:
    """Two gaps say two different things; a count alone would not.

    "Coverage was incomplete" tells a reader nothing they can act on. Which
    feed went missing is what tells them whether the trace could have seen
    an ERC-20 hop or a contract-delivered one.
    """
    adapter = adapter_serving(Capability.ADDRESS_HISTORY)

    page = await adapter.address_history(ADDRESS)

    assert {gap.capability for gap in page.gaps} == {
        Capability.TOKEN_TRANSFERS,
        Capability.INTERNAL_TRACES,
    }
    assert {gap.code for gap in page.gaps} == {
        "feed_unavailable:token_transfers",
        "feed_unavailable:internal_traces",
    }
    summaries = {gap.summary for gap in page.gaps}
    assert len(summaries) == 2  # each names its own feed, in words
    assert all(gap.detail for gap in page.gaps)  # and says why it was lost


async def test_a_degraded_page_still_normalizes() -> None:
    """The surviving rows keep their provenance when a sibling feed is gone.

    A missing feed leaves no provenance to stamp on its movements, so the
    dialect carries None for it. Normalizing must go on producing the
    movements that DID arrive rather than tripping over the absence.
    """
    adapter = adapter_serving(Capability.ADDRESS_HISTORY, Capability.INTERNAL_TRACES)

    page = await adapter.address_history(ADDRESS)
    movements = [m for item in page.items for m in (await adapter.normalize(item)).movements]

    assert movements
    assert all(m.kind is not MovementKind.TOKEN for m in movements)
    assert all(len(m.provenance.payload_sha256) == 64 for m in movements)


def test_a_feed_gap_must_name_its_chain() -> None:
    """A gap that cannot say where it happened is not evidence of anything."""
    with pytest.raises(ValueError):
        FeedGap(chain="", capability=Capability.TOKEN_TRANSFERS)
