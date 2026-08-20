"""Explorer tags: a name channel that must never become an evidence channel.

The through-line of this file is one property, asserted from several angles: a
name fetched from a public explorer is visible to an investigator and invisible
to everything that concludes. The unit tests cover the fetch and the shape
rules; ``test_never_reaches_the_attributor`` is the one that would fail if the
whole design were quietly undone.
"""

from __future__ import annotations

from datetime import UTC, datetime

import httpx
import pytest

from cipherchain.intel.explorer_tags import (
    READERS,
    SUPPORTED_CHAINS,
    ExplorerTag,
    _clean_tag,
    claim_from_tag,
    fetch_tron_tag,
    lookup_tags,
)
from cipherchain.intel.policy import TRUSTED_METHODS, arrival_status

T0 = datetime(2026, 8, 20, 12, 0, tzinfo=UTC)
TAGGED = "TU4vEruvZwLLkSfV9bNw12EJTPvNr7Pvaa"


def _client(handler: object) -> httpx.AsyncClient:
    return httpx.AsyncClient(transport=httpx.MockTransport(handler))  # type: ignore[arg-type]


def _json(payload: object, status: int = 200) -> object:
    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(status, json=payload)

    return handler


# ---------------------------------------------------------------- shape rules


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("Bybit", "Bybit"),
        ("  Binance   2 ", "Binance 2"),
        ("", None),
        ("   ", None),
        (None, None),
        (12, None),
    ],
)
def test_clean_tag(raw: object, expected: str | None) -> None:
    assert _clean_tag(raw) == expected


def test_poisoned_entity_is_refused_not_stored() -> None:
    """The exact string that closed community feeds must not survive.

    ``Binance (successor wallet 0xATTACKER)`` stems to "binance" and would
    corroborate against real Binance data. IntelClaim refuses it; this asserts
    the refusal is a None the caller can skip, not an exception that would
    abandon the other addresses in the batch.
    """
    poisoned = ExplorerTag(
        chain="tron",
        address=TAGGED,
        tag="Binance (successor wallet 0xATTACKER)",
        source="tronscan-public-tag",
    )
    assert claim_from_tag(poisoned, now=T0) is None


def test_overlong_entity_is_refused() -> None:
    long = ExplorerTag("tron", TAGGED, "A" * 65, "tronscan-public-tag")
    assert claim_from_tag(long, now=T0) is None


def test_claim_arrives_pending_by_construction() -> None:
    """Not "we set status pending" — the METHOD forces it.

    If somebody later changed TAG_METHOD to a trusted one, this is the test
    that notices, because it re-derives the status through policy rather than
    reading back a constant.
    """
    claim = claim_from_tag(ExplorerTag("tron", TAGGED, "Bybit", "tronscan-public-tag"), now=T0)
    assert claim is not None
    assert claim.method not in TRUSTED_METHODS
    assert arrival_status(claim.method) == "pending"
    assert claim.category == "vasp"


# -------------------------------------------------------------------- fetching


async def test_fetch_returns_the_tag() -> None:
    async with _client(_json({"addressTag": "Bybit"})) as http:
        tag = await fetch_tron_tag(TAGGED, http=http, api_key=None)
    assert tag == ExplorerTag("tron", TAGGED, "Bybit", "tronscan-public-tag")


async def test_untagged_address_is_a_miss_not_an_error() -> None:
    async with _client(_json({"addressTag": ""})) as http:
        assert await fetch_tron_tag(TAGGED, http=http, api_key=None) is None


@pytest.mark.parametrize("status", [401, 403, 429, 500])
async def test_non_200_is_a_miss(status: int) -> None:
    async with _client(_json({"addressTag": "Bybit"}, status)) as http:
        assert await fetch_tron_tag(TAGGED, http=http, api_key=None) is None


async def test_network_failure_is_a_miss() -> None:
    """An explorer being down costs leads, never the investigation."""

    def boom(_request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("no route to host")

    async with _client(boom) as http:
        assert await fetch_tron_tag(TAGGED, http=http, api_key=None) is None


async def test_unparseable_body_is_a_miss() -> None:
    def html(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, text="<html>maintenance</html>")

    async with _client(html) as http:
        assert await fetch_tron_tag(TAGGED, http=http, api_key=None) is None


async def test_api_key_selects_the_documented_endpoint() -> None:
    """Keyless uses the legacy path; a key uses accountv2 and sends the header.

    Worth pinning because the keyless endpoint is the one that answers today
    and the keyed one is the supported route — a silent swap of either would be
    invisible until a deployment with a key got 401s.
    """
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(200, json={"addressTag": "Bybit"})

    async with _client(handler) as http:
        await fetch_tron_tag(TAGGED, http=http, api_key=None)
        await fetch_tron_tag(TAGGED, http=http, api_key="secret")

    assert seen[0].url.path.endswith("/api/account")
    assert "TRON-PRO-API-KEY" not in seen[0].headers
    assert seen[1].url.path.endswith("/api/accountv2")
    assert seen[1].headers["TRON-PRO-API-KEY"] == "secret"


# -------------------------------------------------------------------- batching


async def test_supported_chains_is_derived_from_the_readers() -> None:
    """A chain can never be declared supported without a reader for it.

    The earlier version kept the two as separate constants, which would have
    sent Ethereum addresses at a Tron endpoint and reported "nobody knew them".
    """
    assert frozenset(READERS) == SUPPORTED_CHAINS


async def test_unsupported_chain_looks_nothing_up() -> None:
    calls: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        return httpx.Response(200, json={"addressTag": "Bybit"})

    async with _client(handler) as http:
        assert await lookup_tags(["0xabc"], chain="ethereum", http=http, spacing=0) == []
    assert calls == []


async def test_cap_bounds_the_requests() -> None:
    calls: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        return httpx.Response(200, json={"addressTag": "Bybit"})

    async with _client(handler) as http:
        found = await lookup_tags(
            [f"T{i}" for i in range(10)], chain="tron", http=http, max_lookups=3, spacing=0
        )
    assert len(calls) == 3
    assert len(found) == 3


async def test_misses_are_dropped_and_order_preserved() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        address = request.url.params["address"]
        tag = {"T1": "Bybit", "T3": "MXC"}.get(address, "")
        return httpx.Response(200, json={"addressTag": tag})

    async with _client(handler) as http:
        found = await lookup_tags(["T1", "T2", "T3"], chain="tron", http=http, spacing=0)
    assert [t.address for t in found] == ["T1", "T3"]
    assert [t.tag for t in found] == ["Bybit", "MXC"]
