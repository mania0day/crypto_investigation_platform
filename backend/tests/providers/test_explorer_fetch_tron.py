"""The fetch tier on Tron: pages in, the body ``TronAdapter`` actually reads out.

Tron is the chain this tier was missing, and missing it cost more than any
other: a Tron trace had exactly ONE provider (TronGrid, priority 10), because
Blockscout is EVM-only and this tier declined the chain. A spent or throttled
TronGrid did not slow a Tron trace down, it ended it — on the run that prompted
this work, 1589 of 1592 addresses carried no label at all.

Two things are pinned here that the Ethereum tests cannot pin.

**The body is asserted against the real adapter, not against an idea of it.**
Every shape test drives ``TronAdapter`` over a real ``ProviderPool`` with this
provider as the only registered source, and reads the ``Movement`` that comes
out the far end. A row that merely looks TronGrid-shaped is not the property
that matters; a row the adapter turns into the right movement is. The row keys
are additionally checked against the recorded TronGrid payloads in
``tests/chains/fixtures``, which are the bytes the adapter was written against.

**The stop is honest.** TronGrid pages on an opaque ``meta.fingerprint`` and
nothing on a numbered explorer page can mint one, so this tier reports a single
finished page rather than a cursor nobody could follow — and it declines a
request that arrives carrying somebody else's cursor instead of answering it
from page one. A false "there is more" and a false "that was everything" are
both lies about the same page; the second is the cheaper one, and it is the one
that gets recorded in the manifest and the log.

The Tron pages here are SYNTHETIC, built to 3xpl's recorded Ethereum layout
(seven panes, a checked radio naming the active one, transfer blocks under an
``<h1>`` section heading). No Tron page from 3xpl has been recorded, so what
these prove is the emitter and the dialect, not the site's Tron markup.
"""

from __future__ import annotations

import json
from collections.abc import Callable
from pathlib import Path
from typing import Any

import httpx
import pytest

from cipherchain.chains.tron import TRX_ASSET, TronAdapter
from cipherchain.core.errors import AllProvidersFailed, ProviderResponseInvalid
from cipherchain.core.models import Address, Capability, MovementKind
from cipherchain.providers.base import SHORT_READ_KEY, ProviderRequest
from cipherchain.providers.clients.explorer_fetch import (
    DEFAULT_SITES,
    ETHERSCAN_ROWS,
    TRONGRID_ROWS,
    ExplorerFetchProvider,
)
from cipherchain.providers.pool import ProviderLimits, ProviderPool
from tests.providers.test_explorer_fetch import ALLOW_ALL, Clock, asset_page, build

# Real Tron addresses, taken from the recorded TronGrid payloads so that every
# Base58 string in this file carries a valid checksum: an address invented by
# hand would pass this tier's pattern and fail on the chain.
ADDRESS = "TUTpjYd12MSVqMJUHA8tn571FeFSRKe6V6"
COUNTERPARTY = "TEDLqyiP2s3CxetdMTRFRYm6p1UwaMFxA2"
STRANGER = "TNwe3WoEX6XrQ5NPAeWPkvpTtdaf27XYxK"
USDT = "TR7NHqjeKQxGTCi8q8ZY4pL8otSzgjLj6t"
USDT_ASSET = f"tron-trc-20/{USDT}"

# A Tron transaction id: 64 hex characters and NO `0x`.
TX = "3e96c093cd96b4038b314ce232dea9100fe92632f0fdf0444c7eb34a2c3b3d31"
TOKEN_TX = "9d474d27c880078dbf2dea79dac5168c8f0c3f87fc64a7cbe94bcece7d3c3317"
BLOCK = 85138441
STAMP_MS = 1786086243000

PANES = 7
ACTIVE = 1
HEAD = (
    "<thead><tr><th>Date &amp; time</th><th>Transaction id</th>"
    "<th>Amount</th><th>Status</th></tr></thead>"
)
EMPTY_ROW = '<tr><td colspan="4" class="table__cell_empty">No events</td></tr>'
NOTICE = '<p class="notice"><span>There are no events in this module.</span></p>'

RECORDED = Path(__file__).parents[1] / "chains" / "fixtures"


def recorded(name: str) -> dict[str, Any]:
    """A payload TronGrid really served, as the adapter's own tests use it.

    Read from the chain fixtures rather than copied here: the point of the
    comparison is that this tier's rows and the vendor's rows are the same
    shape, and a copy would let them drift apart the moment either is edited.
    """
    body: dict[str, Any] = json.loads((RECORDED / name).read_text())
    return body


def placeholder_pane() -> str:
    return f'<div class="tab-content"><table>{HEAD}<tbody>{EMPTY_ROW}</tbody></table>{NOTICE}</div>'


def address_page(hashes: list[str], *, prefix: str = "") -> str:
    """One module pane among seven, the rest placeholders.

    ``prefix`` puts an EVM-style ``0x`` in front of each id, which is the shape
    this dialect must NOT read as a Tron transaction.
    """
    rows = "".join(
        f'<tr><td><a class="hash__hash" '
        f'href="https://3xpl.com/tron/transaction/{prefix}{h}">{h}</a></td></tr>'
        for h in hashes
    )
    body = rows or EMPTY_ROW
    module = f'<div class="tab-content"><table>{HEAD}<tbody>{body}</tbody></table></div>'
    radios = "".join(
        '<input class="tabs__radio-button" type="radio" name="address-module-events"'
        + (" checked" if index == ACTIVE else "")
        + f' id="address-module-events-{index}">'
        for index in range(PANES)
    )
    panes = "".join(module if index == ACTIVE else placeholder_pane() for index in range(PANES))
    return f'<html><body>{radios}<div class="tabs__content">{panes}</div></body></html>'


def empty_page() -> str:
    """The site answering "no events in this module" in the pane asked about —
    the only shape that is allowed to produce zero rows."""
    radios = "".join(
        '<input class="tabs__radio-button" type="radio" name="address-module-events"'
        + (" checked" if index == ACTIVE else "")
        + f' id="address-module-events-{index}">'
        for index in range(PANES)
    )
    panes = "".join(
        f'<div class="tab-content">{NOTICE}</div>' if index == ACTIVE else placeholder_pane()
        for index in range(PANES)
    )
    return f'<html><body>{radios}<div class="tabs__content">{panes}</div></body></html>'


def transfer(sender: str, recipient: str, amount: str, asset: str, symbol: str) -> str:
    addresses = "".join(
        f'<div class="even-transfer__address-group"><div class="even-transfer__hash">'
        f'<a class="link_semibold hash__hash" '
        f'href="https://3xpl.com/tron/address/{who}">{who}</a>'
        f"</div></div>"
        for who in (sender, recipient)
    )
    return (
        '<div class="even-transfer mono-p1">'
        f'<div class="even-transfer__addresses-row">{addresses}</div>'
        '<div class="even-transfer__values-row"><div class="even-transfer__values">'
        '<p class="even-transfer-values__caption">Amount transferred:</p>'
        f'<p class=" color-text_secondary ">{amount}'
        f'<span class="currency"><span class="currency__name-wrap">'
        f'<a href="https://3xpl.com/asset/{asset}" class="currency__name">{symbol}</a>'
        f'<span class="tooltip">Id: {asset}\nName: {symbol} Token</span></span>'
        "</span></p>"
        '<p class="even-transfer-values__delimiter">.</p>'
        '<p class=" color-text_secondary ">1.00 USD</p>'
        "</div></div></div>"
    )


def transaction_page(sections: dict[str, str]) -> str:
    body = "".join(
        f"<h1>{title}</h1><section>{content}</section>" for title, content in sections.items()
    )
    return (
        "<html><body>"
        f'<span data-watch-time data-start-timestamp="{STAMP_MS}">now</span>'
        f'<a data-block href="https://3xpl.com/tron/block/{BLOCK}">{BLOCK}</a>'
        f"{body}</body></html>"
    )


def additional_events(text: str) -> str:
    """The notice 3xpl collapses a transaction's absent modules into. It NAMES
    them, and the name is the only thing separating "this transaction has no
    TRC-20 events" from "we no longer recognise the heading they are under"."""
    return f'<p class="notice"><span class="notice__text">{text}</span></p>'


def trx_transfer_page(amount: str = "1234.567890") -> str:
    return transaction_page({"Main events": transfer(ADDRESS, COUNTERPARTY, amount, "tron", "TRX")})


def usdt_transfer_page(amount: str = "119610.000000") -> str:
    return transaction_page(
        {"TRC-20 events": transfer(COUNTERPARTY, ADDRESS, amount, USDT_ASSET, "USDT")}
    )


def router(
    pages: dict[str, str], *, robots: str = ALLOW_ALL
) -> Callable[[httpx.Request], httpx.Response]:
    """Route a Tron fetch. Any address page not named explicitly answers the
    "no events in this module" notice, so a module with nothing on it is the
    site SAYING none rather than a page this tier failed to read."""

    def routes(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if path == "/robots.txt":
            return httpx.Response(200, text=robots)
        if path in pages:
            return httpx.Response(200, text=pages[path])
        if "/address/" in path:
            return httpx.Response(200, text=empty_page())
        return httpx.Response(404, text="not found")

    return routes


def module_path(module: str, address: str = ADDRESS, page: int = 0) -> str:
    return f"/tron/address/{address}/{module}/events/{page}"


def traced(
    pages: dict[str, str], *, seen: list[str] | None = None, pages_per_call: int = 2
) -> TronAdapter:
    """A Tron adapter whose ONLY provider is this tier.

    Which is the state the tier exists for: TronGrid spent or throttled, and
    nothing else in the pool serving Tron at all.
    """
    clock = Clock()
    provider = build(router(pages), clock=clock, seen=seen, site_pages_per_call=pages_per_call)
    pool = ProviderPool(clock=clock, sleep=clock.sleep)
    pool.register(provider, limits=ProviderLimits(rate_per_sec=100, burst=100), priority=95)
    return TronAdapter(pool)


class TestTheBodyTheAdapterReads:
    async def test_a_native_trx_transfer_survives_into_a_movement(self) -> None:
        """The whole point of the emitter, end to end: a 3xpl page goes in and
        a TRX movement comes out of the real adapter.

        Every field here is one the adapter would drop the transfer over. It
        reads `data`, then `txID`, then `raw_data.contract[]` for a
        `TransferContract`, then `ret[].contractRet == "SUCCESS"` — a row
        missing any one of them normalizes to no movements at all, which
        reaches the engine as a transaction that moved nothing.
        """
        adapter = traced(
            {
                module_path("tron-main"): address_page([TX]),
                f"/tron/transaction/{TX}": trx_transfer_page(),
            }
        )
        page = await adapter.address_history(Address("tron", ADDRESS))
        assert [tx.tx_hash for tx in page.items] == [TX]

        normalized = await adapter.normalize(page.items[0])
        (movement,) = normalized.movements
        assert movement.kind is MovementKind.NATIVE
        assert movement.asset == TRX_ASSET
        # 1234.567890 TRX is 1234567890 sun at the 6 decimals the chain SDK
        # states, and nothing else. A scale read off the printed digits instead
        # would be a figure wrong by a power of ten in a report a court reads.
        assert movement.amount == 1234567890
        assert movement.from_address is not None
        assert movement.from_address.value == ADDRESS
        assert movement.to_address is not None
        assert movement.to_address.value == COUNTERPARTY
        assert normalized.tx.block_number == BLOCK
        assert int(normalized.tx.timestamp.timestamp() * 1000) == STAMP_MS

    async def test_a_trc20_transfer_survives_into_a_movement(self) -> None:
        """TRC-20 is the feed that matters on Tron — the chain is mostly USDT,
        so an address whose token feed is missing has essentially no visible
        money on it.

        The scale comes off the asset page, as it does for ERC-20: `assets`
        keys on (chain, kind, contract) with `decimals` OUTSIDE the key, so a
        guessed 18 here would redefine USDT-TRC20 for every row TronGrid ever
        wrote.
        """
        adapter = traced(
            {
                module_path("tron-trc-20"): address_page([TOKEN_TX]),
                f"/tron/transaction/{TOKEN_TX}": usdt_transfer_page(),
                f"/asset/{USDT_ASSET}": asset_page(USDT_ASSET, "6"),
            }
        )
        page = await adapter.address_history(Address("tron", ADDRESS))
        assert [tx.tx_hash for tx in page.items] == [TOKEN_TX]
        assert page.gaps == ()  # both feeds answered; neither was lost

        normalized = await adapter.normalize(page.items[0])
        (movement,) = normalized.movements
        assert movement.kind is MovementKind.TOKEN
        assert movement.amount == 119610000000
        assert movement.asset.contract == USDT
        assert movement.asset.symbol == "USDT"
        assert movement.asset.decimals == 6
        assert movement.from_address is not None
        assert movement.from_address.value == COUNTERPARTY
        assert movement.to_address is not None
        assert movement.to_address.value == ADDRESS

    async def test_both_legs_of_one_transaction_merge_under_one_hash(self) -> None:
        """The adapter groups the two feeds by transaction id — `txID` on the
        native row, `transaction_id` on the token row. Spelling either key the
        other way splits one transaction into two, and the graph gains a
        transfer that never happened."""
        adapter = traced(
            {
                module_path("tron-main"): address_page([TX]),
                module_path("tron-trc-20"): address_page([TX]),
                f"/tron/transaction/{TX}": transaction_page(
                    {
                        "Main events": transfer(ADDRESS, COUNTERPARTY, "1.000000", "tron", "TRX"),
                        "TRC-20 events": transfer(
                            ADDRESS, COUNTERPARTY, "50.000000", USDT_ASSET, "USDT"
                        ),
                    }
                ),
                f"/asset/{USDT_ASSET}": asset_page(USDT_ASSET, "6"),
            }
        )
        page = await adapter.address_history(Address("tron", ADDRESS))
        assert len(page.items) == 1

        normalized = await adapter.normalize(page.items[0])
        kinds = sorted(m.kind for m in normalized.movements)
        assert kinds == sorted([MovementKind.NATIVE, MovementKind.TOKEN])

    async def test_a_token_section_that_never_names_the_address_raises(self) -> None:
        """The address's own TRC-20 pane named this transaction, so a section
        holding transfers between other people is the two pages contradicting
        each other. It is also what a case-folded Base58 comparison looks like
        from here — the address stops matching either end of its own transfer —
        so answering "no tokens on this transaction" would empty the feed that
        carries almost all of Tron's value, one transaction at a time."""
        pages = {
            module_path("tron-trc-20", address=STRANGER): address_page([TOKEN_TX]),
            f"/tron/transaction/{TOKEN_TX}": usdt_transfer_page(),
            f"/asset/{USDT_ASSET}": asset_page(USDT_ASSET, "6"),
        }
        provider = build(router(pages), site_pages_per_call=2)
        with pytest.raises(ProviderResponseInvalid, match=f"none with {STRANGER} at either end"):
            await provider.execute(
                ProviderRequest("tron", Capability.TOKEN_TRANSFERS, {"address": STRANGER})
            )

        page = await traced(pages).address_history(Address("tron", STRANGER))
        # ADDRESS_HISTORY is the hard feed and it answered (an empty pane); the
        # token feed is the one that raised, so its loss is RECORDED rather than
        # silently absent.
        assert page.items == ()
        assert [gap.capability for gap in page.gaps] == [Capability.TOKEN_TRANSFERS]

    async def test_an_address_with_nothing_on_it_is_an_empty_page_not_an_error(self) -> None:
        """Both panes carry the site's own "no events in this module" notice.
        That is the site answering none, and it is the only thing allowed to
        produce zero rows."""
        adapter = traced({})
        page = await adapter.address_history(Address("tron", ADDRESS))
        assert page.items == ()
        assert page.next_cursor is None


class TestTheHeadingsNobodyHasReadOffATronPage:
    """``TRC-20 events`` and the ``TRC-20`` the empty notice names are the two
    strings in this dialect that were NOT measured — no 3xpl Tron transaction
    page has been recorded here, so both are the Ethereum layout with the token
    standard swapped.

    Inferring them is only defensible because a wrong guess RAISES. These are
    the tests that hold that: the failure they rule out is the one from the
    Ethereum side of this file, where a heading nobody recognises plus a notice
    about neighbouring modules returns zero rows for every transaction on the
    address and empties the feed carrying almost all of Tron's value, with no
    error anywhere to show for it.
    """

    def pages(self, notice: str) -> dict[str, str]:
        return {
            module_path("tron-trc-20"): address_page([TOKEN_TX]),
            f"/tron/transaction/{TOKEN_TX}": transaction_page(
                {
                    "Main events": transfer(ADDRESS, COUNTERPARTY, "1.000000", "tron", "TRX"),
                    "Additional events": additional_events(notice),
                }
            ),
        }

    async def test_a_transaction_with_no_trc20_events_says_so_by_name(self) -> None:
        """The site naming TRC-20 as absent is the site ANSWERING none, and the
        only thing allowed to produce zero token rows."""
        provider = build(
            router(self.pages("There are no events of Internal, TRC-10 and TRC-20 types.")),
            site_pages_per_call=2,
        )
        response = await provider.execute(
            ProviderRequest("tron", Capability.TOKEN_TRANSFERS, {"address": ADDRESS})
        )
        assert response.payload == {"data": []}

    async def test_a_notice_naming_only_trc10_is_not_an_answer_about_trc20(self) -> None:
        """A notice about the module next door proves nothing about this one —
        and this is exactly the shape a mis-inferred heading takes: the TRC-20
        section is sitting on the page under a name this dialect does not
        know."""
        provider = build(
            router(self.pages("There are no events of Internal and TRC-10 types.")),
            site_pages_per_call=2,
        )
        with pytest.raises(ProviderResponseInvalid, match="naming 'TRC-20' as absent"):
            await provider.execute(
                ProviderRequest("tron", Capability.TOKEN_TRANSFERS, {"address": ADDRESS})
            )


class TestTheShapeAgainstRecordedTronGrid:
    """The rows this tier emits, against rows TronGrid really served.

    The adapter tests above prove the fields the adapter reads. These prove the
    emitter did not invent a key BESIDE them — a row carrying `txId` next to a
    correct `txID` parses fine today and is a trap for whoever reads the two
    dialects side by side tomorrow.
    """

    async def native_row(self) -> dict[str, Any]:
        provider = build(
            router(
                {
                    module_path("tron-main"): address_page([TX]),
                    f"/tron/transaction/{TX}": trx_transfer_page(),
                }
            ),
            site_pages_per_call=2,
        )
        body = (
            await provider.execute(
                ProviderRequest("tron", Capability.ADDRESS_HISTORY, {"address": ADDRESS})
            )
        ).payload
        row: dict[str, Any] = body["data"][0]
        return row

    async def token_row(self) -> dict[str, Any]:
        provider = build(
            router(
                {
                    module_path("tron-trc-20"): address_page([TOKEN_TX]),
                    f"/tron/transaction/{TOKEN_TX}": usdt_transfer_page(),
                    f"/asset/{USDT_ASSET}": asset_page(USDT_ASSET, "6"),
                }
            ),
            site_pages_per_call=2,
        )
        body = (
            await provider.execute(
                ProviderRequest("tron", Capability.TOKEN_TRANSFERS, {"address": ADDRESS})
            )
        ).payload
        row: dict[str, Any] = body["data"][0]
        return row

    async def test_the_envelope_is_the_one_trongrid_serves(self) -> None:
        provider = build(router({}), site_pages_per_call=2)
        body = (
            await provider.execute(
                ProviderRequest("tron", Capability.ADDRESS_HISTORY, {"address": ADDRESS})
            )
        ).payload
        # A list here is the failure this dialect exists to prevent: the adapter
        # calls `.get("data")` on whatever arrives.
        assert isinstance(body, dict)
        assert body["data"] == []

    async def test_no_native_key_is_one_trongrid_never_sends(self) -> None:
        row = await self.native_row()
        vendor = recorded("tron_native.json")["data"][0]
        assert set(row) <= set(vendor)
        contract = row["raw_data"]["contract"][0]
        assert set(contract) <= set(vendor["raw_data"]["contract"][0])
        assert set(contract["parameter"]) <= set(vendor["raw_data"]["contract"][0]["parameter"])
        assert set(contract["parameter"]["value"]) <= set(
            vendor["raw_data"]["contract"][0]["parameter"]["value"]
        )
        assert contract["type"] == vendor["raw_data"]["contract"][0]["type"]

    async def test_no_token_key_is_one_trongrid_never_sends(self) -> None:
        row = await self.token_row()
        vendor = recorded("tron_trc20.json")["data"][0]
        assert set(row) <= set(vendor)
        assert set(row["token_info"]) <= set(vendor["token_info"])
        # A string, like the vendor's. The adapter reads it through `int()`, so
        # the type is free — and free is exactly when two dialects drift.
        assert isinstance(row["value"], str)
        assert isinstance(vendor["value"], str)

    async def test_the_success_marker_is_the_one_the_adapter_demands(self) -> None:
        """`_succeeded` reads `ret[].contractRet` and an absent `ret` is read as
        failure, so a row without this stamp normalizes to zero movements — a
        transaction that reaches the engine having moved nothing at all."""
        row = await self.native_row()
        assert row["ret"] == [{"contractRet": "SUCCESS"}]


class TestPagingItCannotFake:
    async def test_a_finished_page_is_reported_as_finished(self) -> None:
        adapter = traced(
            {
                module_path("tron-main"): address_page([TX]),
                f"/tron/transaction/{TX}": trx_transfer_page(),
            }
        )
        page = await adapter.address_history(Address("tron", ADDRESS))
        # No `meta.links.next` in either body, so `_join_cursor` has nothing to
        # carry: the adapter stops here instead of asking for a page this tier
        # could not have positioned.
        assert page.next_cursor is None

    async def test_a_short_read_says_so_where_a_reviewer_will_see_it(self) -> None:
        """The site still had pages and the caller has no cursor to ask with.

        The rows returned are real, but the READ is short — and a short read
        that says nothing is indistinguishable from an address whose history
        ends here. So it is recorded in the manifest the digest covers, which is
        the thing a reviewer refetches the derivation from.
        """
        provider = build(
            router(
                {
                    module_path("tron-main", page=0): address_page([TX]),
                    module_path("tron-main", page=1): address_page([TOKEN_TX]),
                    f"/tron/transaction/{TX}": trx_transfer_page(),
                    f"/tron/transaction/{TOKEN_TX}": trx_transfer_page(),
                }
            ),
            site_pages_per_call=2,
        )
        response = await provider.execute(
            ProviderRequest("tron", Capability.ADDRESS_HISTORY, {"address": ADDRESS})
        )
        assert json.loads(response.raw)["truncated_at_page_limit"] == 2

    async def test_a_read_that_reached_the_end_claims_no_truncation(self) -> None:
        provider = build(
            router(
                {
                    module_path("tron-main", page=0): address_page([TX]),
                    f"/tron/transaction/{TX}": trx_transfer_page(),
                }
            ),
            site_pages_per_call=2,
        )
        response = await provider.execute(
            ProviderRequest("tron", Capability.ADDRESS_HISTORY, {"address": ADDRESS})
        )
        assert "truncated_at_page_limit" not in json.loads(response.raw)

    async def test_the_short_read_is_also_in_the_body_the_adapter_unwraps(self) -> None:
        """The manifest is where a REVIEWER checks the derivation. No adapter
        parses it, so a truncation recorded only there reaches nothing above
        this provider — which is how a Tron address read ten explorer pages deep
        arrived in the report as an address whose history ends. The envelope is
        the copy that travels, and it is the copy the provider cache replays.
        """
        provider = build(
            router(
                {
                    module_path("tron-main", page=0): address_page([TX]),
                    module_path("tron-main", page=1): address_page([TOKEN_TX]),
                    f"/tron/transaction/{TX}": trx_transfer_page(),
                    f"/tron/transaction/{TOKEN_TX}": trx_transfer_page(),
                }
            ),
            site_pages_per_call=2,
        )
        body = (
            await provider.execute(
                ProviderRequest("tron", Capability.ADDRESS_HISTORY, {"address": ADDRESS})
            )
        ).payload
        assert body[SHORT_READ_KEY] is True
        # Everything TronGrid itself sends is untouched: the adapter reads the
        # same one dialect whether this tier or TronGrid answered.
        assert isinstance(body["data"], list) and body["data"]

    async def test_a_finished_body_carries_no_short_read_mark(self) -> None:
        """The mark is the one key TronGrid never sends, so it must appear only
        when it is true — a body that always carried it would report every Tron
        address as partly read and empty the caveat of its meaning."""
        provider = build(
            router(
                {
                    module_path("tron-main", page=0): address_page([TX]),
                    f"/tron/transaction/{TX}": trx_transfer_page(),
                }
            ),
            site_pages_per_call=2,
        )
        body = (
            await provider.execute(
                ProviderRequest("tron", Capability.ADDRESS_HISTORY, {"address": ADDRESS})
            )
        ).payload
        assert SHORT_READ_KEY not in body

    async def test_a_short_read_arrives_at_the_adapter_as_a_truncated_page(self) -> None:
        """End to end over the real adapter, which is the layer the engine sees.

        ``next_cursor`` is None here and always will be — this tier cannot mint
        a fingerprint — and ``gaps`` is empty because both feeds answered. Those
        are the only two things the engine used to test, so this page was
        counted as a COMPLETE read of a busy address, and the report's "N
        address(es) had more history than was read" omitted exactly the
        addresses where a VASP is likeliest.
        """
        adapter = traced(
            {
                module_path("tron-main", page=0): address_page([TX]),
                module_path("tron-main", page=1): address_page([TOKEN_TX]),
                f"/tron/transaction/{TX}": trx_transfer_page(),
                f"/tron/transaction/{TOKEN_TX}": trx_transfer_page(),
            }
        )
        page = await adapter.address_history(Address("tron", ADDRESS))
        assert page.next_cursor is None
        assert page.gaps == ()
        assert page.truncated is True

    async def test_a_cut_token_feed_alone_truncates_the_page(self) -> None:
        """A page is the MERGE of the two feeds, and the caller reads one
        address rather than two feeds. Tron is mostly USDT, so the feed most
        likely to run past the page bound is the one whose loss matters most."""
        adapter = traced(
            {
                module_path("tron-trc-20", page=0): address_page([TX]),
                module_path("tron-trc-20", page=1): address_page([TOKEN_TX]),
                f"/tron/transaction/{TX}": usdt_transfer_page(),
                f"/tron/transaction/{TOKEN_TX}": usdt_transfer_page(),
                f"/asset/{USDT_ASSET}": asset_page(USDT_ASSET, "6"),
            }
        )
        page = await adapter.address_history(Address("tron", ADDRESS))
        assert page.gaps == (), "both feeds answered; this is a short read, not a dead feed"
        assert page.truncated is True

    async def test_a_whole_read_reaches_the_adapter_as_a_whole_page(self) -> None:
        adapter = traced(
            {
                module_path("tron-main"): address_page([TX]),
                f"/tron/transaction/{TX}": trx_transfer_page(),
            }
        )
        page = await adapter.address_history(Address("tron", ADDRESS))
        assert page.truncated is False

    async def test_a_trongrid_cursor_is_declined_before_a_page_is_fetched(self) -> None:
        """A continuation this tier cannot position is refused, not approximated.

        Answering it from page one would hand back rows the caller already has
        and then report the feed finished — a hole in the middle of a history,
        presented as a completed read. The decline costs the continuation and
        nothing else, and it costs no crawl budget: not even robots.txt is
        fetched, because the request was never answerable.
        """
        seen: list[str] = []
        provider = build(router({}), seen=seen, site_pages_per_call=2)
        request = ProviderRequest(
            "tron",
            Capability.ADDRESS_HISTORY,
            {"address": ADDRESS, "fingerprint": "9zPiuPCd7WjmQgW8M4A2s5Lvi9WvVN6UzPLd57dkWSbU"},
        )
        response = await provider.execute(request)
        assert response.payload["data"] == []
        assert seen == []
        # The cursor is named in the manifest and NO document is listed under
        # it. A reviewer refetching this derivation has to find nothing to
        # refetch and be told why, or a zero-row answer reads as a page that was
        # read and held nothing.
        manifest = json.loads(response.raw)
        assert manifest["declined_continuation"].startswith("9zPiuPCd")
        assert manifest["documents"] == []

    async def test_the_decline_is_an_answer_and_not_a_provider_failure(self) -> None:
        """The decline used to raise `ProviderResponseInvalid`, and `ProviderPool`
        charges that class to the breaker.

        The breaker belongs to a REGISTERED PROVIDER and this tier is registered
        once for every chain it serves (`runtime.build_provider_pool`), so five
        declined Tron continuations opened the circuit and took ETHEREUM's floor
        tier down with it — over a question Ethereum never asked. What comes
        back now is the honest answer to what was asked: no rows, marked as a
        read cut short.
        """
        # Every address page answers the "no events in this module" notice, so
        # the Ethereum read below is the SITE saying none rather than a page
        # this tier failed to read.
        clock = Clock()
        seen: list[str] = []
        provider = build(router({}), clock=clock, seen=seen, site_pages_per_call=1)
        pool = ProviderPool(clock=clock, sleep=clock.sleep)
        # Exactly the runtime registration: ONE registration serving both chains,
        # at the default failure threshold of 5 that the declines used to reach.
        pool.register(provider, limits=ProviderLimits(rate_per_sec=100, burst=100), priority=95)

        # Six, one past the threshold. Distinct cursors because that is how a
        # caller asks — one per page — so the count is six declines and not one
        # decline asked six times.
        for attempt in range(6):
            body = (
                await pool.fetch(
                    ProviderRequest(
                        "tron",
                        Capability.ADDRESS_HISTORY,
                        {"address": ADDRESS, "fingerprint": f"9zPiuPCd7WjmQgW8M4A2s5Lvi{attempt}"},
                    )
                )
            ).payload
            assert body["data"] == []
            assert body[SHORT_READ_KEY] is True
        assert seen == [], "a decline this tier cannot answer must cost no crawl budget"

        # Ethereum was answering fine throughout and must still be — proved by
        # the site actually being read, which an open circuit would have skipped
        # (the pool refuses the provider outright and raises AllProvidersFailed).
        served = await pool.fetch(
            ProviderRequest("ethereum", Capability.ADDRESS_HISTORY, {"address": "0xabc"})
        )
        assert served.payload == []
        assert any("/ethereum/address/0xabc/" in url for url in seen)

    async def test_a_declined_continuation_reaches_the_adapter_as_a_short_read(self) -> None:
        """What the decline costs, stated where the engine can act on it.

        Zero rows, `truncated` True: the address was read in part, which is the
        truth and is what `mark_history_truncated` needs. Failing instead — the
        old ending — cost strictly more than the continuation, because the
        adapter awaits its native feed hard and the pool's `AllProvidersFailed`
        took the whole address down with the one page nobody could position.
        """
        adapter = traced(
            {
                module_path("tron-main"): address_page([TX]),
                f"/tron/transaction/{TX}": trx_transfer_page(),
            }
        )
        page = await adapter.address_history(Address("tron", ADDRESS), cursor="somefingerprint|")
        assert page.items == ()
        assert page.next_cursor is None
        assert page.truncated is True
        # NOT a feed gap. Both feeds answered — with "I cannot position this
        # cursor", which is a short read and not a feed nobody could serve.
        assert page.gaps == ()

    async def test_the_ethereum_dialect_still_pages_by_number(self) -> None:
        """The decline is a property of the Tron dialect, not of the tier. EVM
        callers page by an incrementing integer this tier can honour, and
        breaking that would take the Ethereum floor out with it."""
        assert ETHERSCAN_ROWS.caller_pages_by_number
        assert not TRONGRID_ROWS.caller_pages_by_number


class TestTronIdentifiers:
    def test_a_tron_id_and_an_evm_id_are_not_the_same_string(self) -> None:
        """Neither pattern may match the other's ids. An EVM pattern on a Tron
        page finds no transactions, and no transactions on an address page is
        this tier's worst answer: an address with history reported as having
        none."""
        assert ETHERSCAN_ROWS.tx_href.search(f"/transaction/{TX}") is None
        assert TRONGRID_ROWS.tx_href.search(f"/transaction/0x{TX}") is None
        assert TRONGRID_ROWS.tx_href.search(f"/transaction/{TX}") is not None
        assert ETHERSCAN_ROWS.address_href.search(f"/address/{ADDRESS}") is None
        assert TRONGRID_ROWS.address_href.search(f"/address/{ADDRESS}") is not None

    async def test_an_evm_shaped_id_is_never_read_as_a_tron_transaction(self) -> None:
        """If 3xpl ever renders Tron ids `0x`-prefixed, this tier must raise
        rather than report the rows it can no longer read as no history."""
        adapter = traced({module_path("tron-main"): address_page([TX], prefix="0x")})
        with pytest.raises(AllProvidersFailed):
            await adapter.address_history(Address("tron", ADDRESS))

    async def test_a_base58_address_is_never_case_folded(self) -> None:
        """The EVM dialect lower-cases addresses, because EVM hex is
        case-insensitive. Base58Check is not: folding a Tron address produces a
        string that fails its own checksum, so the page 404s and every token
        transfer stops matching the address it belongs to."""
        seen: list[str] = []
        adapter = traced(
            {
                module_path("tron-main"): address_page([TX]),
                f"/tron/transaction/{TX}": trx_transfer_page(),
            },
            seen=seen,
        )
        await adapter.address_history(Address("tron", ADDRESS))
        assert any(f"/tron/address/{ADDRESS}/tron-main/" in url for url in seen)
        assert not any(ADDRESS.lower() in url for url in seen)


class TestTheTable:
    def test_the_modules_are_the_slugs_read_off_the_live_page(self) -> None:
        """Read on 2026-08-16, in the same seven-pane strip Ethereum uses. A
        guessed slug would 404 into a parse error on every call."""
        site = DEFAULT_SITES["tron"]
        assert site.host == "3xpl.com"
        assert site.path_chain == "tron"
        assert site.modules[Capability.ADDRESS_HISTORY] == "tron-main"
        assert site.modules[Capability.TOKEN_TRANSFERS] == "tron-trc-20"

    def test_trx_decimals_are_the_chain_sdk_s_and_not_a_second_opinion(self) -> None:
        """`native_decimals` scales every TRX amount this tier emits, and the
        adapter builds every TRX movement from `TRX_ASSET`. Two constants for
        one fact is one power-of-ten error waiting to happen, so this is what
        keeps them the same fact."""
        assert DEFAULT_SITES["tron"].native_decimals == TRX_ASSET.decimals

    def test_internal_and_trc10_are_verified_slugs_left_unmapped(self) -> None:
        """`tron-internal` and `tron-trc-10` were read off the same live page.
        Neither is mapped, and that is deliberate: `TronAdapter` asks the pool
        for ADDRESS_HISTORY and TOKEN_TRANSFERS and nothing else, so a row
        emitted for either would reach no adapter and be compared against no
        vendor body — and a shape nobody consumes is a shape nobody notices
        going wrong. The reason is the consumer and not the vendor: TronGrid
        does carry internal transfers, embedded in the native row as
        `internal_transactions`, and nothing here reads them from there
        either."""
        site = DEFAULT_SITES["tron"]
        assert set(site.modules) == {Capability.ADDRESS_HISTORY, Capability.TOKEN_TRANSFERS}

    def test_the_tier_now_answers_for_tron_and_only_where_it_can(self) -> None:
        """The gap this closes: before it, Blockscout (90) declined Tron and
        this tier (95) declined it too, so a Tron trace had TronGrid or it had
        nothing."""
        provider = ExplorerFetchProvider(httpx.AsyncClient())
        assert provider.supports("tron", Capability.ADDRESS_HISTORY)
        assert provider.supports("tron", Capability.TOKEN_TRANSFERS)
        assert not provider.supports("tron", Capability.INTERNAL_TRACES)
        assert not provider.supports("tron", Capability.TX_LOOKUP)
