"""Tron loses its TRC-20 feed without losing the address.

The same regression the EVM adapter carried, on the chain where it costs the
most. Tron is mostly USDT, so an address whose token feed died has almost no
visible money left — and Tron is where the label packs carry the most VASPs, so
a page killed by one dead feed is a VASP not reached.

The pool is real: a provider that declares only some capabilities is exactly how
a spent pool behaves, so the unserved feed fails through the genuine
``AllProvidersFailed`` path with nothing monkeypatched.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

import pytest

from cipherchain.chains.tron import TronAdapter
from cipherchain.core.errors import AllProvidersFailed
from cipherchain.core.hashing import canonical_json_bytes, sha256_hex
from cipherchain.core.models import Address, Capability, MovementKind
from cipherchain.providers.base import Provider, ProviderRequest, ProviderResponse
from cipherchain.providers.cache import InMemoryCache
from cipherchain.providers.pool import ProviderPool
from tests.chains.conftest import fixture_json

ADDRESS = Address("tron", "TR7NHqjeKQxGTCi8q8ZY4pL8otSzgjLj6t")

BODY_BY_CAPABILITY = {
    Capability.ADDRESS_HISTORY: "tron_native.json",
    Capability.TOKEN_TRANSFERS: "tron_trc20.json",
}


class PartialProvider(Provider):
    """Serves the feeds it declares and no others — a spent pool, honestly."""

    name = "partial-tron"

    def __init__(self, served: set[Capability]) -> None:
        self._served = served

    def supports(self, chain: str, capability: Capability) -> bool:
        return chain == "tron" and capability in self._served

    async def execute(self, request: ProviderRequest) -> ProviderResponse:
        payload: dict[str, Any] = fixture_json(BODY_BY_CAPABILITY[request.capability])
        raw = canonical_json_bytes(payload)
        return ProviderResponse(
            provider=self.name,
            retrieved_at=datetime(2026, 8, 16, tzinfo=UTC),
            payload=payload,
            raw=raw,
            payload_sha256=sha256_hex(raw),
        )


def adapter_serving(*served: Capability) -> TronAdapter:
    pool = ProviderPool(cache=InMemoryCache())
    pool.register(PartialProvider(set(served)), priority=10)
    return TronAdapter(pool)


async def test_a_healthy_pool_reports_no_gap() -> None:
    page = await adapter_serving(
        Capability.ADDRESS_HISTORY, Capability.TOKEN_TRANSFERS
    ).address_history(ADDRESS)
    assert page.gaps == ()
    assert page.complete is True
    assert page.items


async def test_losing_the_trc20_feed_keeps_the_address_and_names_the_loss() -> None:
    adapter = adapter_serving(Capability.ADDRESS_HISTORY)
    page = await adapter.address_history(ADDRESS)

    # The address still has a page — native transfers survived.
    assert page.items, "a dead token feed took the whole address with it"
    assert page.complete is False
    assert len(page.gaps) == 1
    gap = page.gaps[0]
    assert gap.chain == "tron"
    assert gap.capability is Capability.TOKEN_TRANSFERS
    assert gap.code == "feed_unavailable:token_transfers"
    assert "token transfers" in gap.summary

    # And what survived still normalizes, rather than tripping over the
    # provenance of a feed that never answered.
    for item in page.items:
        normalized = await adapter.normalize(item)
        assert all(m.kind is not MovementKind.TOKEN for m in normalized.movements)


async def test_losing_the_native_feed_still_raises() -> None:
    """With no native feed there is nothing to merge into.

    Returning an empty page would tell the engine this address never
    transacted, which is the one answer that must never be invented.
    """
    with pytest.raises(AllProvidersFailed):
        await adapter_serving(Capability.TOKEN_TRANSFERS).address_history(ADDRESS)


async def test_a_feed_that_was_never_read_is_not_a_feed_that_is_finished() -> None:
    """The cursor must not retire the token feed it failed to fetch.

    ``_join_cursor`` reads "no next link" out of the empty body of a request
    that was never made. Left alone, that records the token feed as exhausted
    and skips it for every remaining page of the address — a permanent hole
    opened by one transient outage.
    """
    adapter = adapter_serving(Capability.ADDRESS_HISTORY)
    page = await adapter.address_history(ADDRESS, cursor="native-mark|token-mark")
    assert page.next_cursor is not None
    assert page.next_cursor.endswith("token-mark"), (
        "the mark we failed at was dropped, so the next page starts past it"
    )
