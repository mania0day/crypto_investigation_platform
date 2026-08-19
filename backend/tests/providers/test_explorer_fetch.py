"""The fetch tier: reachable only after every keyed provider is spent.

Three things are pinned harder than anything else here.

**A false empty must be impossible.** A real address page renders seven tab
panes and fills only the one named in the URL; the other six are placeholders
that always look empty — six "No events" tables on the recorded page-1 fixture,
which holds ten transactions. So zero rows must be proved by the ACTIVE pane
and by nothing else, or an address with history is reported as having none the
moment a row stops being recognised. Every test that produces zero rows proves
that pane said zero. One level down, on the transaction page, the same rule:
the "Additional events" notice has to NAME the module asked about, because a
notice about ERC-721 is not an answer about ERC-20.

**A token amount is exact or it is an error.** The scale comes off the asset
page, which states it, and the printed precision has to agree with it. Nothing
is inferred from digit counts and nothing defaults to 18 — ``assets`` keys on
(chain, kind, contract) with ``decimals`` outside the key, so one wrong scale
would redefine that contract for every row a keyed provider ever wrote.

**The boundaries are behaviour, not documentation.** robots.txt is fetched
before any path, a disallow declines, an unreadable robots declines, and a
Crawl-delay can only slow the tier down. Those are tests, so nobody can
loosen them by editing a docstring.

The HTML fixtures are real pages recorded from 3xpl on 2026-08-16 (see
``fixtures/manifest.json``), stored gzipped exactly as served.
"""

from __future__ import annotations

import gzip
import json
from collections.abc import Callable
from pathlib import Path

import httpx
import pytest

from cipherchain.core.errors import (
    ProviderRateLimited,
    ProviderResponseInvalid,
    ProviderUnavailable,
)
from cipherchain.core.models import Capability
from cipherchain.providers.base import ProviderRequest
from cipherchain.providers.clients.explorer_fetch import (
    DEFAULT_RATE_PER_SEC,
    DEFAULT_SITES,
    ExplorerFetchProvider,
    ExplorerSite,
    RobotsPolicy,
)

FIXTURES = Path(__file__).parent / "fixtures"
ADDRESS = "0xee7ae85f2fe2239e27d9c1e23fffe168d63b4055"
# The case study address. Its ONLY ERC-20 event is an unverified token, which
# is why it appears here rather than a tidier one.
CASE_ADDRESS = "0xdcbeffbecce100cce9e4b153c4e15cb885643193"
USDC = "ethereum-erc-20/0xa0b86991c6218b36c1d19d4a2e9eb0ce3606eb48"
YRISE = "ethereum-erc-20/0x6051c1354ccc51b4d561e43b02735deae64768b8"
ALLOW_ALL = "User-agent: *\nAllow: /\n"


def fixture(name: str) -> str:
    return gzip.decompress((FIXTURES / f"{name}.html.gz").read_bytes()).decode()


class Clock:
    """Monotonic time the test advances itself, so a tier that waits five real
    seconds between pages runs instantly here."""

    def __init__(self) -> None:
        self.now = 0.0
        self.waits: list[float] = []

    def __call__(self) -> float:
        return self.now

    async def sleep(self, seconds: float) -> None:
        self.waits.append(seconds)
        self.now += seconds


def build(
    routes: Callable[[httpx.Request], httpx.Response],
    *,
    clock: Clock | None = None,
    seen: list[str] | None = None,
    **kwargs: object,
) -> ExplorerFetchProvider:
    clock = clock or Clock()

    def handler(request: httpx.Request) -> httpx.Response:
        if seen is not None:
            seen.append(str(request.url))
        return routes(request)

    http = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    return ExplorerFetchProvider(
        http,
        clock=clock,
        sleep=clock.sleep,
        **kwargs,  # type: ignore[arg-type]
    )


PANES = 7  # six event modules plus the "recent transactions" pseudo-tab
ACTIVE = 1  # the pane 3xpl fills for /ethereum-main/events/N
HEAD = (
    "<thead><tr><th>Date &amp; time</th><th>Transaction id</th>"
    "<th>Amount</th><th>Status</th></tr></thead>"
)
EMPTY_ROW = '<tr><td colspan="4" class="table__cell_empty">No events</td></tr>'
NOTICE = '<p class="notice"><span>There are no events in this module.</span></p>'


def placeholder_pane() -> str:
    """A pane for a module nobody asked about.

    3xpl renders these as an empty table on some pages and as the notice on
    others, and the recorded pages carry both — so the stand-in carries both
    too. Neither says anything about the module in the URL, and a parser that
    lets one of them stand as proof reports a busy address as having no
    history the moment it stops recognising a row.
    """
    return f'<div class="tab-content"><table>{HEAD}<tbody>{EMPTY_ROW}</tbody></table>{NOTICE}</div>'


def address_page(
    hashes: list[str],
    *,
    empty_marker: bool = True,
    notice: bool = False,
    active_pane: bool = True,
) -> str:
    """A stand-in for the real module page, with the structure that decides:
    seven panes, the requested module's singled out by the checked radio, and
    placeholders around it that always look empty."""
    rows = "".join(
        f'<tr><td><a class="hash__hash" '
        f'href="https://3xpl.com/ethereum/transaction/{h}">{h}</a></td></tr>'
        for h in hashes
    )
    body = rows or (EMPTY_ROW if empty_marker else "")
    module = (
        f'<div class="tab-content">{NOTICE}</div>'
        if notice
        else f'<div class="tab-content"><table>{HEAD}<tbody>{body}</tbody></table></div>'
    )
    radios = "".join(
        '<input class="tabs__radio-button" type="radio" name="address-module-events"'
        + (" checked" if index == ACTIVE else "")
        + f' id="address-module-events-{index}">'
        for index in range(PANES)
    )
    panes = "".join(
        module if index == ACTIVE and active_pane else placeholder_pane() for index in range(PANES)
    )
    return f'<html><body>{radios}<div class="tabs__content">{panes}</div></body></html>'


def transfer(
    sender: str,
    recipient: str,
    amount: str,
    asset: str = "ethereum",
    *,
    symbol: str = "SYM",
    linked: bool = True,
) -> str:
    """One transfer block. ``linked=False`` is the unverified-token shape: 3xpl
    renders the currency name as a bare span and states the asset id only in
    the hover tooltip, so the href is not there to read."""
    addresses = "".join(
        f'<div class="even-transfer__address-group"><div class="even-transfer__hash">'
        f'<a class="link_semibold hash__hash" '
        f'href="https://3xpl.com/ethereum/address/{who}">{who}</a>'
        f"</div></div>"
        for who in (sender, recipient)
    )
    name = (
        f'<a href="https://3xpl.com/asset/{asset}" class="currency__name">{symbol}</a>'
        if linked
        else f'<span class="currency__name">{symbol}</span>'
    )
    return (
        '<div class="even-transfer mono-p1">'
        f'<div class="even-transfer__addresses-row">{addresses}</div>'
        '<div class="even-transfer__values-row"><div class="even-transfer__values">'
        '<p class="even-transfer-values__caption">Amount transferred:</p>'
        f'<p class=" color-text_secondary ">{amount}'
        f'<span class="currency"><span class="currency__name-wrap">{name}'
        f'<span class="tooltip">Id: {asset}\nName: {symbol} Token</span></span>'
        '<span class="currency__verified"><span class="tooltip">Verified currency</span></span>'
        "</span></p>"
        '<p class="even-transfer-values__delimiter">.</p>'
        '<p class=" color-text_secondary ">1.00 USD</p>'
        "</div></div></div>"
    )


def no_events_of(*modules: str) -> str:
    """The notice 3xpl prints in place of the sections a transaction has none
    of. It NAMES them, and the naming is what makes it an answer: without it a
    renamed heading empties a whole feed and nothing anywhere errors."""
    return f"<p>There are no events of {', '.join(modules)} types.</p>"


def transaction_page(
    sections: dict[str, str], *, block: int = 42, ts_ms: int = 1786025147000
) -> str:
    body = "".join(
        f"<h1>{title}</h1><section>{content}</section>" for title, content in sections.items()
    )
    return (
        "<html><body>"
        f'<span data-watch-time data-start-timestamp="{ts_ms}">now</span>'
        f'<a data-block href="https://3xpl.com/ethereum/block/{block}">{block}</a>'
        f"{body}</body></html>"
    )


def native_tx(amount: str = "1.000000000000000000") -> str:
    """A readable transaction page for tests about pagination and provenance,
    where WHICH transaction it is does not matter but being parseable does."""
    return transaction_page({"Main events": transfer("0x" + "a" * 40, "0x" + "b" * 40, amount)})


def asset_page(asset_id: str, decimals: str) -> str:
    """The page that states a contract's scale, which is the only reason this
    tier can answer about tokens at all."""
    return (
        "<html><body>"
        '<section class="currency-section"><h2 class="section-title">ID</h2>'
        f'<div class="mono-p2 currency-section__value"><svg></svg><p>{asset_id}</p></div>'
        "</section>"
        '<section class="currency-section"><h2 class="section-title">Decimals</h2>'
        f'<p class="mono-p2 currency-description__value">{decimals}</p></section>'
        '<section class="currency-section"><h2 class="section-title">Supply</h2>'
        '<p class="mono-p2 currency-description__value">1,000 SYM</p></section>'
        "</body></html>"
    )


def router(
    pages: dict[str, str], *, robots: str = ALLOW_ALL
) -> Callable[[httpx.Request], httpx.Response]:
    def routes(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if path == "/robots.txt":
            return httpx.Response(200, text=robots)
        if path in pages:
            return httpx.Response(200, text=pages[path])
        return httpx.Response(404, text="not found")

    return routes


def history(page: int = 1, capability: Capability = Capability.ADDRESS_HISTORY) -> ProviderRequest:
    return ProviderRequest(
        chain="ethereum", capability=capability, params={"address": ADDRESS, "page": page}
    )


def tokens(address: str = ADDRESS, page: int = 1) -> ProviderRequest:
    return ProviderRequest(
        chain="ethereum",
        capability=Capability.TOKEN_TRANSFERS,
        params={"address": address, "page": page},
    )


class TestBoundaries:
    def test_it_serves_every_feed_an_evm_history_call_asks_for(self) -> None:
        """`EvmAdapter.address_history` fetches all three capabilities on every
        call. A tier serving two of them was no floor under the pool at all:
        once the keyed providers and Blockscout were spent, an EVM trace died
        on the token fetch instead of slowing down — and the case study turns
        on 119,610 USDT from Binance, which is exactly the feed that was
        missing."""
        provider = build(router({}))
        assert provider.supports("ethereum", Capability.ADDRESS_HISTORY)
        assert provider.supports("ethereum", Capability.TOKEN_TRANSFERS)
        assert provider.supports("ethereum", Capability.INTERNAL_TRACES)
        assert not provider.supports("bitcoin", Capability.ADDRESS_HISTORY)

    async def test_a_disallowed_path_is_declined_before_it_is_requested(self) -> None:
        seen: list[str] = []
        provider = build(router({}, robots="User-agent: *\nDisallow: /ethereum/\n"), seen=seen)
        with pytest.raises(ProviderResponseInvalid, match="declined"):
            await provider.execute(history())
        assert seen == ["https://3xpl.com/robots.txt"]

    async def test_a_robots_redirect_declines_the_whole_host(self) -> None:
        """Etherscan's live behaviour: robots.txt answers a redirect into a
        challenge, so the rules are unknown. Unknown is not permission."""

        def routes(request: httpx.Request) -> httpx.Response:
            if request.url.path == "/robots.txt":
                return httpx.Response(302, headers={"Location": "/challenge"})
            return httpx.Response(200, text="<html></html>")

        provider = build(routes)
        with pytest.raises(ProviderResponseInvalid, match="rules unknown"):
            await provider.execute(history())

    async def test_an_unreachable_robots_fails_closed(self) -> None:
        def routes(request: httpx.Request) -> httpx.Response:
            if request.url.path == "/robots.txt":
                raise httpx.ConnectError("no route to host")
            return httpx.Response(200, text="<html></html>")

        provider = build(routes)
        with pytest.raises(ProviderResponseInvalid, match="unreachable"):
            await provider.execute(history())

    async def test_a_site_publishing_no_robots_is_an_allow_all(self) -> None:
        pages = {
            f"/ethereum/address/{ADDRESS}/ethereum-main/events/0": address_page([]),
        }

        def routes(request: httpx.Request) -> httpx.Response:
            if request.url.path == "/robots.txt":
                return httpx.Response(404)
            return router(pages)(request)

        provider = build(routes, site_pages_per_call=1)
        response = await provider.execute(history())
        assert response.payload == []

    async def test_robots_is_read_once_per_host_not_once_per_page(self) -> None:
        seen: list[str] = []
        hashes = ["0x" + f"{i:064x}" for i in range(3)]
        pages = {f"/ethereum/address/{ADDRESS}/ethereum-main/events/0": address_page(hashes)}
        pages.update({f"/ethereum/transaction/{h}": native_tx() for h in hashes})
        provider = build(router(pages), seen=seen, site_pages_per_call=1)
        await provider.execute(history())
        assert seen.count("https://3xpl.com/robots.txt") == 1

    async def test_a_crawl_delay_may_only_slow_the_tier_down(self) -> None:
        """A site asking for one request every 5s gets one every 5s. A site
        asking for one every 0.1s still gets our own conservative rate — the
        politeness floor is ours, the ceiling is theirs."""
        hashes = ["0x" + f"{i:064x}" for i in range(2)]
        pages = {f"/ethereum/address/{ADDRESS}/ethereum-main/events/0": address_page(hashes)}
        pages.update({f"/ethereum/transaction/{h}": native_tx() for h in hashes})
        slow = Clock()
        provider = build(
            router(pages, robots="User-agent: *\nAllow: /\nCrawl-delay: 5\n"),
            clock=slow,
            site_pages_per_call=1,
        )
        await provider.execute(history())
        assert max(slow.waits) == pytest.approx(5.0)

        fast = Clock()
        provider = build(
            router(pages, robots="User-agent: *\nAllow: /\nCrawl-delay: 0.05\n"),
            clock=fast,
            site_pages_per_call=1,
        )
        await provider.execute(history())
        # Derived from the constant, not restated: the default rate is a
        # measurement of what 3xpl permits (see DEFAULT_RATE_PER_SEC), and a test
        # that hard-codes it turns any future re-measurement into a test failure
        # that looks like a regression.
        assert max(fast.waits) == pytest.approx(1.0 / DEFAULT_RATE_PER_SEC)


class TestTheSiteFightingBack:
    """What 3xpl actually does when this tier reads too fast.

    Measured against the live site on 2026-08-16, at the 1 req/s this module
    used to default to: over budget the site answers 429 with a full "Verify you
    are not a robot" interstitial, and if the crawl continues it answers 403
    with ``Retry-After: 3600`` — an hour-long block on the whole host. The tier
    that exists so an exhausted quota SLOWS a trace was reliably ending its own
    access on the first address it read.
    """

    def test_the_default_rate_stays_inside_the_budget_the_site_states(self) -> None:
        """3xpl serves ``x-ratelimit-limit: 25``. Exceeding it is not a throttle,
        it is a one-hour ban, so the default has to sit under 25/min with room."""
        assert DEFAULT_RATE_PER_SEC * 60 < 25

    async def test_a_rate_limit_is_a_slow_down_and_not_a_dead_tier(self) -> None:
        """429 must reach the pool as ProviderRateLimited.

        ProviderResponseInvalid is never retried and fails straight over, so
        mapping a throttle to it would discard the last tier in the pool the
        first time a site asked us to wait — turning "read it slower" into
        AllProvidersFailed, which is the one outcome this module exists to avoid.
        """

        def routes(request: httpx.Request) -> httpx.Response:
            if request.url.path == "/robots.txt":
                return httpx.Response(200, text=ALLOW_ALL)
            return httpx.Response(429, text="<html>Verify you are not a robot</html>")

        provider = build(routes)
        with pytest.raises(ProviderRateLimited):
            await provider.execute(history())

    async def test_a_block_says_the_whole_site_is_shut_not_that_a_page_is_odd(self) -> None:
        """403 is the escalation, and it has to read as one.

        "HTTP 403 for /ethereum/address/…" sends an operator to the parser while
        every capability on every chain is dark for an hour. The error names the
        host, the ban and its duration instead.
        """

        def routes(request: httpx.Request) -> httpx.Response:
            if request.url.path == "/robots.txt":
                return httpx.Response(200, text=ALLOW_ALL)
            return httpx.Response(403, text="shadowed veil", headers={"Retry-After": "3600"})

        provider = build(routes)
        with pytest.raises(ProviderResponseInvalid) as caught:
            await provider.execute(history())
        message = str(caught.value)
        assert "3xpl.com is refusing this host" in message
        assert "3600" in message

    async def test_a_moved_page_says_it_moved(self) -> None:
        """Redirects are not followed, so a 3xx body is not the page.

        It fails the pane check either way — there is no false empty here — but
        "no active module pane" points at the parser, and the page having moved
        is the likelier cause and the cheaper thing to check first.
        """

        def routes(request: httpx.Request) -> httpx.Response:
            if request.url.path == "/robots.txt":
                return httpx.Response(200, text=ALLOW_ALL)
            return httpx.Response(301, headers={"Location": "https://3xpl.com/eth/new"})

        provider = build(routes)
        with pytest.raises(ProviderResponseInvalid) as caught:
            await provider.execute(history())
        assert "redirected" in str(caught.value)
        assert "may have moved" in str(caught.value)

    async def test_the_bot_check_page_is_never_read_as_an_empty_history(self) -> None:
        """The interstitial is 56 KB of valid HTML with no events table in it.

        If it ever arrives with a 200 instead of a 429 it must still raise: an
        address reported as having no history is the one wrong answer nothing
        downstream can detect.
        """
        pages = {
            f"/ethereum/address/{ADDRESS}/ethereum-main/events/0": (
                "<html><body><h1>Verify you are not a robot</h1>"
                "<form><input type='text' name='captcha'></form></body></html>"
            )
        }
        provider = build(router(pages), site_pages_per_call=1)
        with pytest.raises(ProviderResponseInvalid) as caught:
            await provider.execute(history())
        assert "no active module pane" in str(caught.value)


class TestPageCache:
    def pages(self, hashes: list[str]) -> dict[str, str]:
        pages = {
            f"/ethereum/address/{ADDRESS}/ethereum-main/events/0": address_page(hashes),
            f"/ethereum/address/{ADDRESS}/ethereum-trace/events/0": address_page(hashes),
        }
        pages.update(
            {
                f"/ethereum/transaction/{h}": transaction_page(
                    {
                        "Main events": transfer("0x" + "a" * 40, "0x" + "b" * 40, "1." + "0" * 18),
                        "Internal events": transfer(
                            "0x" + "c" * 40, "0x" + "d" * 40, "2." + "0" * 18
                        ),
                    }
                )
                for h in hashes
            }
        )
        return pages

    async def test_one_transaction_page_is_fetched_once_across_capabilities(self) -> None:
        """An EVM history call asks for three capabilities over the same pages.

        Behind a keyed provider fetching each page three times is waste. Against
        a site that permits 25 requests a minute and bans the host for an hour
        past that, it is the difference between a slow trace and no trace.
        """
        hashes = ["0x" + f"{i:064x}" for i in range(3)]
        seen: list[str] = []
        provider = build(router(self.pages(hashes)), seen=seen, site_pages_per_call=1)

        await provider.execute(history())
        await provider.execute(history(capability=Capability.INTERNAL_TRACES))

        for h in hashes:
            fetched = seen.count(f"https://3xpl.com/ethereum/transaction/{h}")
            assert fetched == 1, f"transaction page fetched {fetched} times, not once"

    async def test_an_answer_served_from_cache_still_cites_every_page(self) -> None:
        """The manifest is what lets a reviewer refetch and check the derivation.

        A cached page that dropped out of it would leave rows in the answer whose
        source is named nowhere — evidence with no citation.
        """
        hashes = ["0x" + f"{i:064x}" for i in range(2)]
        provider = build(router(self.pages(hashes)), site_pages_per_call=1)

        first = await provider.execute(history())
        second = await provider.execute(history(capability=Capability.INTERNAL_TRACES))

        for response in (first, second):
            cited = {d["url"] for d in json.loads(response.raw)["documents"]}
            for h in hashes:
                assert f"https://3xpl.com/ethereum/transaction/{h}" in cited
            assert all(d["sha256"] for d in json.loads(response.raw)["documents"])

    async def test_address_pages_are_never_cached(self) -> None:
        """A mined transaction is immutable; an address is not.

        Reusing an address page would pin a trace to whatever history existed the
        first time it was read, and silently miss everything since.
        """
        hashes = ["0x" + f"{i:064x}" for i in range(2)]
        seen: list[str] = []
        provider = build(router(self.pages(hashes)), seen=seen, site_pages_per_call=1)

        await provider.execute(history())
        await provider.execute(history())

        url = f"https://3xpl.com/ethereum/address/{ADDRESS}/ethereum-main/events/0"
        assert seen.count(url) == 2

    async def test_the_cache_is_bounded(self) -> None:
        """A long crawl must not hold every page it has ever read in memory."""
        hashes = ["0x" + f"{i:064x}" for i in range(4)]
        seen: list[str] = []
        provider = build(
            router(self.pages(hashes)),
            seen=seen,
            site_pages_per_call=1,
            transaction_pages_cached=2,
        )
        await provider.execute(history())
        await provider.execute(history(capability=Capability.INTERNAL_TRACES))
        # Four pages through a cache of two: the earliest were evicted and
        # refetched rather than accumulating.
        assert len(provider._pages) == 2
        assert seen.count(f"https://3xpl.com/ethereum/transaction/{hashes[0]}") == 2


class TestAddressPage:
    def address_routes(self, page: str) -> Callable[[httpx.Request], httpx.Response]:
        tx = fixture("3xpl_ethereum_transaction_no_internal")

        def routes(request: httpx.Request) -> httpx.Response:
            if request.url.path == "/robots.txt":
                return httpx.Response(200, text=ALLOW_ALL)
            if "/address/" in request.url.path:
                return httpx.Response(200, text=page)
            return httpx.Response(200, text=tx)

        return routes

    async def test_the_rows_come_from_the_pane_for_the_module_that_was_asked_for(self) -> None:
        """The recorded page carries seven panes; the ten transactions are in
        one of them. Reading "the first table" (an empty placeholder) or "the
        last table" (another empty placeholder) reports this address as having
        no history."""
        seen: list[str] = []
        provider = build(
            self.address_routes(fixture("3xpl_ethereum_address_main_events")),
            seen=seen,
            site_pages_per_call=1,
        )
        await provider.execute(history())
        assert len([url for url in seen if "/transaction/" in url]) == 10

    async def test_six_empty_placeholder_panes_do_not_hide_a_page_of_history(self) -> None:
        """Page 1 of the same address is the page that catches a whole-page
        empty-marker count: every one of the six panes nobody asked about
        renders its own "No events" table, and the module's pane still holds
        ten transactions."""
        seen: list[str] = []
        provider = build(
            self.address_routes(fixture("3xpl_ethereum_address_main_events_page1")),
            seen=seen,
            site_pages_per_call=1,
        )
        await provider.execute(history())
        assert len([url for url in seen if "/transaction/" in url]) == 10

    async def test_a_row_the_parser_stops_recognising_raises_instead_of_emptying(self) -> None:
        """The recorded page with 3xpl's transaction URLs moved. The ten rows
        are still there and still visible to a reader; this tier can no longer
        read them, and the placeholder panes around them are all marked empty.
        Answering "no history" here is the one failure nothing downstream can
        detect, so it has to be an error."""
        page = fixture("3xpl_ethereum_address_main_events").replace("/transaction/", "/tx/")
        provider = build(self.address_routes(page), site_pages_per_call=1)
        with pytest.raises(ProviderResponseInvalid, match="refusing to report"):
            await provider.execute(history())

    async def test_an_address_with_no_history_says_so_in_its_own_pane(self) -> None:
        """The recorded page for an address with nothing on it: the module's
        pane holds the notice "There are no events in this module", which is
        the site answering none. The pane for the pseudo-tab beside it holds an
        empty TABLE, and that one is not an answer about anything."""
        provider = build(
            self.address_routes(fixture("3xpl_ethereum_address_no_events")),
            site_pages_per_call=1,
        )
        response = await provider.execute(history())
        assert response.payload == []

    async def test_a_page_without_an_events_table_is_an_error_not_an_empty_history(self) -> None:
        broken = "<html><body>maintenance</body></html>"
        pages = {f"/ethereum/address/{ADDRESS}/ethereum-main/events/0": broken}
        provider = build(router(pages), site_pages_per_call=1)
        with pytest.raises(ProviderResponseInvalid, match="no events table"):
            await provider.execute(history())

    async def test_a_table_with_neither_rows_nor_an_empty_marker_refuses(self) -> None:
        """The layout changed under us: the header is still there but the rows
        are gone. Reporting "no history" here is the failure mode this tier is
        least allowed to have."""
        pages = {
            f"/ethereum/address/{ADDRESS}/ethereum-main/events/0": address_page(
                [], empty_marker=False
            )
        }
        provider = build(router(pages), site_pages_per_call=1)
        with pytest.raises(ProviderResponseInvalid, match="refusing to report"):
            await provider.execute(history())

    async def test_a_page_that_names_no_active_pane_is_an_error(self) -> None:
        """No checked radio means the page never says which module it answered
        about. Guessing a pane would be guessing whose history this is."""
        pages = {
            f"/ethereum/address/{ADDRESS}/ethereum-main/events/0": address_page([]).replace(
                " checked", ""
            )
        }
        provider = build(router(pages), site_pages_per_call=1)
        with pytest.raises(ProviderResponseInvalid, match="no active module pane"):
            await provider.execute(history())

    async def test_one_extra_pane_cannot_shift_a_busy_page_into_an_empty_one(self) -> None:
        """The active pane is found by ORDINAL — the nth radio selects the nth
        pane — and that only addresses the right pane while the two lists stay
        one-to-one. Add a single `tab-content` div above the strip (a promo
        card, a sidebar widget) and every pane after it shifts by one.

        This is the worst failure this file can have, because it does not look
        like a failure. The pane then read is a placeholder for a module nobody
        asked about, placeholders always look empty, and an empty placeholder
        carrying the events header satisfies `proves_empty` — so the recorded
        page holding TEN transactions comes back as a proven-empty history with
        nothing raised anywhere. Measured before this check existed.
        """
        page = fixture("3xpl_ethereum_address_main_events")
        cut = page.index(">", page.index("<body")) + 1
        shifted = page[:cut] + '<div class="tab-content"><p>Ad slot</p></div>' + page[cut:]
        provider = build(self.address_routes(shifted), site_pages_per_call=1)
        with pytest.raises(ProviderResponseInvalid, match="no active module pane"):
            await provider.execute(history())

    async def test_a_full_page_of_repeated_ids_is_not_mistaken_for_the_last_page(self) -> None:
        """A transaction that both leaves and arrives at the address takes two
        rows. Sizing a page by DISTINCT ids reads that full page as short, stops
        there, and drops every page after it with nothing recording the loss."""
        first = ["0x" + f"{i:064x}" for i in range(4)]
        # four rows, three distinct: the same page size, one id repeated
        repeated = ["0x" + f"{i:064x}" for i in (4, 5, 6, 6)]
        pages = {
            f"/ethereum/address/{ADDRESS}/ethereum-main/events/0": address_page(first),
            f"/ethereum/address/{ADDRESS}/ethereum-main/events/1": address_page(repeated),
            f"/ethereum/address/{ADDRESS}/ethereum-main/events/2": address_page(
                ["0x" + f"{i:064x}" for i in (7, 8, 9, 10)]
            ),
            f"/ethereum/address/{ADDRESS}/ethereum-main/events/3": address_page([]),
        }
        pages.update(
            {
                f"/ethereum/transaction/{h}": native_tx()
                for h in ["0x" + f"{i:064x}" for i in range(11)]
            }
        )
        seen: list[str] = []
        provider = build(router(pages), seen=seen, site_pages_per_call=5)
        await provider.execute(history())
        assert any(url.endswith("/events/2") for url in seen)

    async def test_a_short_page_ends_pagination_without_another_request(self) -> None:
        first = ["0x" + f"{i:064x}" for i in range(4)]
        second = ["0x" + f"{i:064x}" for i in range(4, 6)]
        pages = {
            f"/ethereum/address/{ADDRESS}/ethereum-main/events/0": address_page(first),
            f"/ethereum/address/{ADDRESS}/ethereum-main/events/1": address_page(second),
            f"/ethereum/address/{ADDRESS}/ethereum-main/events/2": address_page([]),
        }
        pages.update({f"/ethereum/transaction/{h}": native_tx() for h in first + second})
        seen: list[str] = []
        provider = build(router(pages), seen=seen, site_pages_per_call=5)
        await provider.execute(history())
        assert not any(url.endswith("/events/2") for url in seen)

    async def test_a_later_logical_page_starts_where_the_previous_one_stopped(self) -> None:
        seen: list[str] = []
        pages = {
            f"/ethereum/address/{ADDRESS}/ethereum-main/events/{n}": address_page([])
            for n in range(9)
        }
        provider = build(router(pages), seen=seen, site_pages_per_call=3)
        await provider.execute(history(page=3))
        assert any(url.endswith("/events/6") for url in seen)


class TestTransactionPage:
    async def test_a_native_transfer_becomes_an_exact_smallest_unit_row(self) -> None:
        """2.97531845 ETH is 2975318450000000000 wei and nothing else. The
        printed decimal is padded to 18 places, so the conversion is exact —
        and a value with more precision than the asset has raises instead of
        rounding."""
        pages = {
            f"/ethereum/address/{ADDRESS}/ethereum-main/events/0": address_page(["0x" + "1" * 64]),
            "/ethereum/transaction/" + "0x" + "1" * 64: fixture(
                "3xpl_ethereum_transaction_with_internal"
            ),
        }
        provider = build(router(pages), site_pages_per_call=1)
        (row,) = (await provider.execute(history())).payload
        assert row["value"] == "2975318450000000000"
        assert row["from"] == "0x2b3fed49557bd88f78b898684f82fbb355305dbb"
        assert row["to"] == "0x09c30cdcdd971423cb3ba757a47d56c35d06d818"
        assert row["blockNumber"] == "25696396"
        assert row["timeStamp"] == "1786025147"

    async def test_internal_transfers_all_survive_into_internal_rows(self) -> None:
        """28 internal transfers in one transaction. `txlistinternal` is a
        list, so every one of them is a row — losing 27 of them would be the
        contract-delivered-value gap all over again."""
        tx = "0x" + "2" * 64
        pages = {
            f"/ethereum/address/{ADDRESS}/ethereum-trace/events/0": address_page([tx]),
            f"/ethereum/transaction/{tx}": fixture("3xpl_ethereum_transaction_with_internal"),
        }
        provider = build(router(pages), site_pages_per_call=1)
        response = await provider.execute(history(capability=Capability.INTERNAL_TRACES))
        assert len(response.payload) == 28
        assert response.payload[0]["value"] == "1753660000000000"
        # No traceId: the caller's fallback key is the transfer's own content,
        # which matches what a keyed provider produced for the same transfer.
        assert "traceId" not in response.payload[0]

    async def test_a_transaction_with_no_internal_section_is_a_proven_empty(self) -> None:
        """The page states "Additional events: there are no events of Internal,
        ERC-721 and ERC-1155 types". That is the site answering none — which is
        allowed to produce zero rows, unlike a section we simply failed to
        find."""
        tx = "0x" + "3" * 64
        pages = {
            f"/ethereum/address/{ADDRESS}/ethereum-trace/events/0": address_page([tx]),
            f"/ethereum/transaction/{tx}": fixture("3xpl_ethereum_transaction_no_internal"),
        }
        provider = build(router(pages), site_pages_per_call=1)
        response = await provider.execute(history(capability=Capability.INTERNAL_TRACES))
        assert response.payload == []

    async def test_a_section_whose_transfer_markup_moved_raises(self) -> None:
        """The heading still prints and not one transfer under it parses. On
        every recorded page a section that exists has content, so this is the
        markup having moved — and "this transaction moved nothing", repeated
        for every transaction on an address, empties a history in silence."""
        tx = "0x" + "b" * 64
        moved = fixture("3xpl_ethereum_transaction_with_internal").replace(
            "even-transfer", "value-transfer"
        )
        pages = {
            f"/ethereum/address/{ADDRESS}/ethereum-main/events/0": address_page([tx]),
            f"/ethereum/transaction/{tx}": moved,
        }
        provider = build(router(pages), site_pages_per_call=1)
        with pytest.raises(ProviderResponseInvalid, match="no readable transfer"):
            await provider.execute(history())

    async def test_a_transaction_page_missing_both_sections_raises(self) -> None:
        tx = "0x" + "4" * 64
        pages = {
            f"/ethereum/address/{ADDRESS}/ethereum-main/events/0": address_page([tx]),
            f"/ethereum/transaction/{tx}": "<html><body><h1>Something else</h1></body></html>",
        }
        provider = build(router(pages), site_pages_per_call=1)
        with pytest.raises(ProviderResponseInvalid, match="no 'Main events' section"):
            await provider.execute(history())

    async def test_a_notice_about_other_modules_is_not_an_answer_about_this_one(self) -> None:
        """The heading moved and the "Additional events" notice is still on the
        page, talking about ERC-721 and ERC-1155. Reading its mere presence as
        "no internal value here" returns zero rows for every transaction on the
        address and empties the contract-delivered-value feed in silence — so
        the notice has to NAME the module that was asked about."""
        tx = "0x" + "c" * 64
        page = transaction_page(
            {
                "Main events": transfer("0x" + "a" * 40, "0x" + "b" * 40, "1.0"),
                "Additional events": no_events_of("ERC-20", "ERC-721", "ERC-1155"),
            }
        )
        pages = {
            f"/ethereum/address/{ADDRESS}/ethereum-trace/events/0": address_page([tx]),
            f"/ethereum/transaction/{tx}": page,
        }
        provider = build(router(pages), site_pages_per_call=1)
        with pytest.raises(ProviderResponseInvalid, match="naming 'Internal' as absent"):
            await provider.execute(history(capability=Capability.INTERNAL_TRACES))

    async def test_a_token_transfer_never_becomes_a_native_row(self) -> None:
        """A token leg under the Main events heading is not native value, and
        rescaling it by the native 18 would be a number nobody could catch. The
        token feed is a separate capability with a separate scale.

        Refusing to emit the row is necessary but not sufficient: returning an
        empty list instead drops a transaction the address's own pane had just
        listed, and says nothing. On ADDRESS_HISTORY that is unrecoverable —
        it is the one feed that must not degrade, so a feed emptied this way
        reaches the engine as "this address never transacted". So the two pages
        disagreeing is an error, which fails over and is recorded, exactly as
        the same contradiction is on the token side.
        """
        tx = "0x" + "5" * 64
        page = transaction_page(
            {
                "Main events": transfer(
                    "0x" + "a" * 40, "0x" + "b" * 40, "1.5", asset="ethereum-erc-20/0x" + "c" * 40
                )
            }
        )
        pages = {
            f"/ethereum/address/{ADDRESS}/ethereum-main/events/0": address_page([tx]),
            f"/ethereum/transaction/{tx}": page,
        }
        provider = build(router(pages), site_pages_per_call=1)
        with pytest.raises(ProviderResponseInvalid, match="none of them in 'ethereum'"):
            await provider.execute(history())

    async def test_an_unverified_token_leg_does_not_make_the_native_feed_unreadable(self) -> None:
        """The recorded airdrop carries 500 ERC-20 transfers and NOT ONE of
        them links its asset — 3xpl only links a verified currency, and states
        an unverified one's id in the hover tooltip. Reading the id from the
        href alone counted all 500 as unreadable and raised on the whole page,
        which took the transaction's native leg down with it. The case study
        address's only ERC-20 event is on this transaction."""
        tx = "0x6bd485ef6bd715cf0d26bb811935c9ce93c17925c25a6f74e75b62d275b3de1e"
        pages = {
            f"/ethereum/address/{CASE_ADDRESS}/ethereum-main/events/0": address_page([tx]),
            f"/ethereum/transaction/{tx}": fixture("3xpl_ethereum_transaction_unverified_token"),
        }
        provider = build(router(pages), site_pages_per_call=1)
        request = ProviderRequest(
            chain="ethereum",
            capability=Capability.ADDRESS_HISTORY,
            params={"address": CASE_ADDRESS, "page": 1},
        )
        (row,) = (await provider.execute(request)).payload
        assert row["from"] == "0x2767ae7e0c205425a7b7f7583c512513c527f482"
        assert row["blockNumber"] == "25709019"

    async def test_two_top_level_transfers_raise_rather_than_overwrite_each_other(self) -> None:
        """A `txlist` row is one per transaction and the caller keys on the
        hash, so a second row would silently replace the first and take its
        value with it."""
        tx = "0x" + "6" * 64
        page = transaction_page(
            {
                "Main events": transfer("0x" + "a" * 40, "0x" + "b" * 40, "1.0")
                + transfer("0x" + "a" * 40, "0x" + "d" * 40, "2.0")
            }
        )
        pages = {
            f"/ethereum/address/{ADDRESS}/ethereum-main/events/0": address_page([tx]),
            f"/ethereum/transaction/{tx}": page,
        }
        provider = build(router(pages), site_pages_per_call=1)
        with pytest.raises(ProviderResponseInvalid, match="expected at most one"):
            await provider.execute(history())

    async def test_an_unreadable_transfer_block_raises_rather_than_shortening_the_list(
        self,
    ) -> None:
        """One endpoint missing means one movement missing, and a movement that
        vanishes takes a counterparty out of the frontier with it. Better a
        loud failure on the whole page."""
        tx = "0x" + "7" * 64
        broken = (
            '<div class="even-transfer mono-p1">'
            '<div class="even-transfer__addresses-row">'
            '<div class="even-transfer__hash"><a class="hash__hash" '
            'href="https://3xpl.com/ethereum/address/0xaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa">a</a>'
            "</div></div></div>"
        )
        pages = {
            f"/ethereum/address/{ADDRESS}/ethereum-main/events/0": address_page([tx]),
            f"/ethereum/transaction/{tx}": transaction_page({"Main events": broken}),
        }
        provider = build(router(pages), site_pages_per_call=1)
        with pytest.raises(ProviderResponseInvalid, match="could not be read"):
            await provider.execute(history())

    async def test_transfers_without_a_block_height_are_refused(self) -> None:
        tx = "0x" + "8" * 64
        page = (
            "<html><body>"
            '<span data-start-timestamp="1786025147000">now</span>'
            "<h1>Main events</h1><section>"
            + transfer("0x" + "a" * 40, "0x" + "b" * 40, "1.0")
            + "</section></body></html>"
        )
        pages = {
            f"/ethereum/address/{ADDRESS}/ethereum-main/events/0": address_page([tx]),
            f"/ethereum/transaction/{tx}": page,
        }
        provider = build(router(pages), site_pages_per_call=1)
        with pytest.raises(ProviderResponseInvalid, match="no block height"):
            await provider.execute(history())


class TestTokenTransfers:
    """The ERC-20 feed, which the EVM adapter asks for on every history call.

    The pane it is read from is a DIFFERENT pane from the native one — index 3
    on a real page rather than index 1 — and the check that finds it is the
    checked radio, not a table ordinal. Its rows are a different shape too:
    they carry a contract, a symbol and a scale instead of a native value.
    """

    def routes_for(
        self, address: str, pane: str, tx_page: str, assets: dict[str, str]
    ) -> Callable[[httpx.Request], httpx.Response]:
        """Route a whole token fetch: one module pane, one transaction page for
        every hash on it, and the asset pages that state the scales."""
        pages = {f"/asset/{asset_id}": page for asset_id, page in assets.items()}

        def routes(request: httpx.Request) -> httpx.Response:
            path = request.url.path
            if path == "/robots.txt":
                return httpx.Response(200, text=ALLOW_ALL)
            if path in pages:
                return httpx.Response(200, text=pages[path])
            if "/address/" in path:
                return httpx.Response(200, text=pane)
            if "/transaction/" in path:
                return httpx.Response(200, text=tx_page)
            return httpx.Response(404, text="not found")

        return routes

    async def test_a_populated_erc20_pane_is_never_read_as_no_token_history(self) -> None:
        """The recorded ERC-20 page holds ten rows in its own pane while a
        placeholder beside it renders an empty "No events" table and five more
        render the "no events in this module" notice. Counting empty markers
        across the page finds six of them and reports an address that received
        tokens as having received none — which on the case study address is the
        difference between naming Binance and reporting a silent chain."""
        seen: list[str] = []
        provider = build(
            self.routes_for(
                ADDRESS,
                fixture("3xpl_ethereum_address_erc20_events"),
                fixture("3xpl_ethereum_transaction_no_internal"),
                {USDC: asset_page(USDC, "6")},
            ),
            seen=seen,
            site_pages_per_call=1,
        )
        response = await provider.execute(tokens())
        # eight distinct ids across ten rows in that pane; every one of them a row
        assert len(response.payload) == 8
        assert len([url for url in seen if "/transaction/" in url]) == 8
        assert all("/ethereum-erc-20/events/" in url for url in seen if "/address/" in url)

    async def test_an_erc20_pane_with_nothing_in_it_is_a_proven_empty(self) -> None:
        """The recorded page for an address with no token history: its own pane
        carries the notice, which is the site answering none. That is allowed to
        produce zero rows — unlike a pane we simply failed to read, which is the
        case above and must raise."""
        provider = build(
            self.routes_for(ADDRESS, fixture("3xpl_ethereum_address_erc20_no_events"), "", {}),
            site_pages_per_call=1,
        )
        assert (await provider.execute(tokens())).payload == []

    async def test_an_unreadable_erc20_row_raises_instead_of_emptying_the_feed(self) -> None:
        """The same recorded page with 3xpl's transaction URLs moved. The rows
        are still visible to a reader; this tier can no longer read them, and
        every placeholder pane around them is marked empty. "No tokens here" is
        the answer nothing downstream can detect as wrong."""
        broken = fixture("3xpl_ethereum_address_erc20_events").replace("/transaction/", "/tx/")
        provider = build(self.routes_for(ADDRESS, broken, "", {}), site_pages_per_call=1)
        with pytest.raises(ProviderResponseInvalid, match="refusing to report"):
            await provider.execute(tokens())

    async def test_a_token_row_carries_what_the_evm_adapter_reads(self) -> None:
        """The adapter builds an Asset out of contractAddress, tokenSymbol and
        tokenDecimal and an amount out of value, and it must not need to know
        which tier served the row — `tokentx` from Etherscan, from Blockscout
        and from here have to be the same envelope."""
        tx = "0x" + "e" * 64
        provider = build(
            self.routes_for(
                ADDRESS,
                address_page([tx]),
                fixture("3xpl_ethereum_transaction_no_internal"),
                {USDC: asset_page(USDC, "6")},
            ),
            site_pages_per_call=1,
        )
        (row,) = (await provider.execute(tokens())).payload
        # 323.799356 USDC at the 6 decimals the asset page states, exactly
        assert row["value"] == "323799356"
        assert row["contractAddress"] == "0xa0b86991c6218b36c1d19d4a2e9eb0ce3606eb48"
        assert row["tokenSymbol"] == "USDC"
        assert row["tokenDecimal"] == "6"
        assert row["tokenName"] == "USD Coin"
        assert row["from"] == ADDRESS
        assert row["to"] == "0xa59b603ab7c769930f8a6f0f24f300da597a3ff5"
        assert row["hash"] == tx
        assert row["timeStamp"].isdigit() and row["blockNumber"].isdigit()

    async def test_an_unverified_token_states_its_contract_only_in_a_tooltip(self) -> None:
        """3xpl links the currency name of a VERIFIED token and renders an
        unverified one as a bare span, so the asset id lives only in the hover
        tooltip. The case study address's single ERC-20 event is one of those:
        reading the id off the href alone made its transaction unparseable."""
        tx = "0x6bd485ef6bd715cf0d26bb811935c9ce93c17925c25a6f74e75b62d275b3de1e"
        provider = build(
            self.routes_for(
                CASE_ADDRESS,
                address_page([tx]),
                fixture("3xpl_ethereum_transaction_unverified_token"),
                {YRISE: asset_page(YRISE, "18")},
            ),
            site_pages_per_call=1,
        )
        (row,) = (await provider.execute(tokens(CASE_ADDRESS))).payload
        assert row["value"] == "90000000000000"  # 0.000090000000000000 at 18 decimals
        assert row["contractAddress"] == "0x6051c1354ccc51b4d561e43b02735deae64768b8"
        assert row["tokenSymbol"] == "yRise"
        assert row["to"] == CASE_ADDRESS

    async def test_an_airdrop_contributes_only_the_leg_the_address_is_on(self) -> None:
        """That same recorded transaction carries 500 ERC-20 transfers between
        501 addresses. `tokentx?address=` means the transfers this address is an
        end of, and emitting the rest would push 500 strangers into the frontier
        off one piece of spam."""
        page = fixture("3xpl_ethereum_transaction_unverified_token")
        assert page.count('class="even-transfer mono-p1"') == 501  # 500 tokens + the native leg
        tx = "0x6bd485ef6bd715cf0d26bb811935c9ce93c17925c25a6f74e75b62d275b3de1e"
        provider = build(
            self.routes_for(
                CASE_ADDRESS, address_page([tx]), page, {YRISE: asset_page(YRISE, "18")}
            ),
            site_pages_per_call=1,
        )
        assert len((await provider.execute(tokens(CASE_ADDRESS))).payload) == 1

    async def test_a_section_that_never_names_the_address_raises_rather_than_drops_it(
        self,
    ) -> None:
        """The address's own ERC-20 pane named this transaction, so a section in
        which it appears nowhere is the two pages contradicting each other.
        Returning nothing for it would leave a hole in a feed that still looked
        complete — the false empty, one transaction at a time."""
        stranger = "0x" + "f" * 40
        tx = "0x6bd485ef6bd715cf0d26bb811935c9ce93c17925c25a6f74e75b62d275b3de1e"
        provider = build(
            self.routes_for(
                stranger,
                address_page([tx]),
                fixture("3xpl_ethereum_transaction_unverified_token"),
                {YRISE: asset_page(YRISE, "18")},
            ),
            site_pages_per_call=1,
        )
        with pytest.raises(ProviderResponseInvalid, match="none with 0xff"):
            await provider.execute(tokens(stranger))

    async def test_a_transaction_with_no_erc20_events_says_so_by_name(self) -> None:
        """The recorded page states "There are no events of ERC-20, ERC-721 and
        ERC-1155 types". That names ERC-20, so it is the site answering none."""
        tx = "0x" + "2" * 64
        provider = build(
            self.routes_for(
                ADDRESS,
                address_page([tx]),
                fixture("3xpl_ethereum_transaction_with_internal"),
                {},
            ),
            site_pages_per_call=1,
        )
        assert (await provider.execute(tokens())).payload == []

    async def test_a_notice_that_does_not_name_erc20_is_not_an_answer_about_it(self) -> None:
        """Rename the "ERC-20 events" heading and every transaction on the
        address finds no section, sees an "Additional events" notice about other
        modules, and contributes nothing. That is a whole token feed emptied
        with no error anywhere — the exact shape of the 119,610 USDT going
        missing."""
        tx = "0x" + "3" * 64
        page = fixture("3xpl_ethereum_transaction_no_internal").replace(
            "ERC-20 events", "Token events"
        )
        provider = build(
            self.routes_for(ADDRESS, address_page([tx]), page, {USDC: asset_page(USDC, "6")}),
            site_pages_per_call=1,
        )
        with pytest.raises(ProviderResponseInvalid, match="naming 'ERC-20' as absent"):
            await provider.execute(tokens())

    async def test_page_furniture_can_never_be_read_as_the_emptiness_notice(self) -> None:
        """The "Additional events" notice is the ONLY thing standing between a
        renamed section heading and a whole token feed reporting zero rows, so
        what counts as that notice has to be exact.

        Gated on the heading alone it runs until the next `<h1>` — and when
        "Additional events" is the last heading on the page that is the footer,
        the inline scripts, everything. 3xpl's own footer carries a link reading
        "ERC-20 tokens", which is enough for a word-bounded search to conclude
        that ERC-20 is absent. Both conditions hold here at once: the heading is
        renamed AND the notice is last, and the page still carries a real
        323.799356 USDC transfer. Bound to its own `notice__text` element, the
        furniture cannot reach the string.
        """
        tx = "0x" + "4" * 64
        page = fixture("3xpl_ethereum_transaction_no_internal").replace(
            "ERC-20 events", "Token events"
        )
        trailing = page.rindex("<h1", 0, page.index("Special data"))
        page = page[:trailing] + '<footer><a href="/tokens">ERC-20 tokens</a></footer></body>'
        provider = build(
            self.routes_for(ADDRESS, address_page([tx]), page, {USDC: asset_page(USDC, "6")}),
            site_pages_per_call=1,
        )
        with pytest.raises(ProviderResponseInvalid, match="naming 'ERC-20' as absent"):
            await provider.execute(tokens())

    async def test_the_scale_is_read_off_the_asset_page_once_per_contract(self) -> None:
        """Decimals are immutable, and re-reading USDT's asset page once per
        transaction would spend the whole crawl budget restating a fact the
        site already gave us — on a tier that runs at one request a second."""
        seen: list[str] = []
        provider = build(
            self.routes_for(
                ADDRESS,
                fixture("3xpl_ethereum_address_erc20_events"),
                fixture("3xpl_ethereum_transaction_no_internal"),
                {USDC: asset_page(USDC, "6")},
            ),
            seen=seen,
            site_pages_per_call=1,
        )
        await provider.execute(tokens())
        assert len([url for url in seen if "/asset/" in url]) == 1

    async def test_a_scale_the_site_does_not_state_is_never_guessed(self) -> None:
        """Not 18, not the digit count, not the last contract's. `assets` keys
        on (chain, kind, contract) with decimals OUTSIDE the key, so a guessed
        scale rewrites that contract for every row a keyed provider wrote — and
        a rescaled amount in a report is not visible to anyone reading it."""
        tx = "0x" + "6" * 64
        silent = asset_page(USDC, "6").replace("Decimals", "Something else")
        provider = build(
            self.routes_for(
                ADDRESS,
                address_page([tx]),
                fixture("3xpl_ethereum_transaction_no_internal"),
                {USDC: silent},
            ),
            site_pages_per_call=1,
        )
        with pytest.raises(ProviderResponseInvalid, match="no 'Decimals' stated"):
            await provider.execute(tokens())

    async def test_an_asset_page_for_a_different_contract_cannot_supply_a_scale(self) -> None:
        """A redirect, or the wrong page cached: its decimals are a fact about
        another token, and USDC's 6 applied to an 18-decimal contract is a
        figure wrong by a factor of a trillion with nothing marking it."""
        tx = "0x" + "7" * 64
        provider = build(
            self.routes_for(
                ADDRESS,
                address_page([tx]),
                fixture("3xpl_ethereum_transaction_no_internal"),
                {USDC: asset_page(YRISE, "18")},
            ),
            site_pages_per_call=1,
        )
        with pytest.raises(ProviderResponseInvalid, match="identifies itself as"):
            await provider.execute(tokens())

    async def test_a_printed_precision_that_contradicts_the_stated_scale_raises(self) -> None:
        """3xpl pads every amount to exactly the asset's decimals — measured on
        6-, 8- and 18-decimal contracts, "54,979,508.000000000000000000"
        included — so the printed precision is a second, independent statement
        of the scale. When the two pages disagree the amount is not converted:
        one of them is wrong and neither can be told which."""
        tx = "0x" + "8" * 64
        provider = build(
            self.routes_for(
                ADDRESS,
                address_page([tx]),
                fixture("3xpl_ethereum_transaction_no_internal"),
                {USDC: asset_page(USDC, "18")},  # the page says 18; "323.799356" says 6
            ),
            site_pages_per_call=1,
        )
        with pytest.raises(ProviderResponseInvalid, match="disagree about this contract"):
            await provider.execute(tokens())

    async def test_a_zero_value_transfer_is_a_row_and_not_a_precision_error(self) -> None:
        """Demanding that the printed precision equal the stated decimals would
        reject a bare "0" and take a whole address's token feed down with one
        piece of zero-value spam. It does not have to: 3xpl pads zero like
        everything else — the recorded airdrop prints its zero native leg as
        "0.000000000000000000" — so the exactness check never meets a bare
        integer on a real page."""
        tx = "0x" + "b" * 64
        page = transaction_page(
            {
                "ERC-20 events": transfer(
                    ADDRESS, "0x" + "d" * 40, "0.000000", asset=USDC, symbol="USDC"
                )
            }
        )
        provider = build(
            self.routes_for(ADDRESS, address_page([tx]), page, {USDC: asset_page(USDC, "6")}),
            site_pages_per_call=1,
        )
        (row,) = (await provider.execute(tokens())).payload
        assert row["value"] == "0"

    async def test_a_symbol_that_cannot_be_read_is_not_replaced_by_a_placeholder(self) -> None:
        """`assets` keys on (chain, kind, contract) with the symbol outside the
        key too, so a row falling back to "TOKEN" renames that contract for
        every row a keyed provider wrote. A transfer whose currency has no name
        is unreadable, not anonymous."""
        tx = "0x" + "9" * 64
        page = fixture("3xpl_ethereum_transaction_no_internal").replace("currency__name", "cn")
        provider = build(
            self.routes_for(ADDRESS, address_page([tx]), page, {USDC: asset_page(USDC, "6")}),
            site_pages_per_call=1,
        )
        with pytest.raises(ProviderResponseInvalid, match="could not be read"):
            await provider.execute(tokens())

    async def test_the_asset_pages_are_in_the_manifest_the_digest_covers(self) -> None:
        """The scale is a claim this answer rests on, so the page it came from
        has to be refetchable by whoever checks the derivation — otherwise the
        manifest documents the transfer and hides the arithmetic."""
        tx = "0x" + "a" * 64
        provider = build(
            self.routes_for(
                ADDRESS,
                address_page([tx]),
                fixture("3xpl_ethereum_transaction_no_internal"),
                {USDC: asset_page(USDC, "6")},
            ),
            site_pages_per_call=1,
        )
        response = await provider.execute(tokens())
        assert f"https://3xpl.com/asset/{USDC}" in response.raw.decode()


class TestProvenance:
    async def test_every_answer_names_the_host_it_was_read_from(self) -> None:
        """`explorer-fetch:<host>`, never the bare tier name: a reader has to
        be able to see which host a conclusion leans on, and filter the tier
        out of a report if they do not want to rely on it."""
        pages = {f"/ethereum/address/{ADDRESS}/ethereum-main/events/0": address_page([])}
        provider = build(router(pages), site_pages_per_call=1)
        response = await provider.execute(history())
        assert response.provider == "explorer-fetch:3xpl.com"
        assert response.provenance().provider == "explorer-fetch:3xpl.com"

    async def test_the_digest_addresses_every_page_that_was_read(self) -> None:
        """The answer is assembled from many pages, so `raw` is the manifest of
        those pages with each one's digest — a reviewer can refetch the same
        URLs and check the derivation instead of trusting the assembled rows."""
        tx = "0x" + "9" * 64
        pages = {
            f"/ethereum/address/{ADDRESS}/ethereum-main/events/0": address_page([tx]),
            f"/ethereum/transaction/{tx}": native_tx(),
        }
        provider = build(router(pages), site_pages_per_call=1)
        response = await provider.execute(history())
        body = response.raw.decode()
        assert f"https://3xpl.com/ethereum/address/{ADDRESS}/ethereum-main/events/0" in body
        assert f"https://3xpl.com/ethereum/transaction/{tx}" in body
        assert len(response.payload_sha256) == 64


class TestTransport:
    async def test_a_server_error_is_an_outage_and_a_refusal_is_not(self) -> None:
        """5xx is retried and counts against the breaker; 4xx is the site
        saying no, and knocking again at a closed door is the behaviour this
        tier is built to avoid."""

        def five_hundred(request: httpx.Request) -> httpx.Response:
            if request.url.path == "/robots.txt":
                return httpx.Response(200, text=ALLOW_ALL)
            return httpx.Response(503)

        provider = build(five_hundred, site_pages_per_call=1)
        with pytest.raises(ProviderUnavailable):
            await provider.execute(history())

        def forbidden(request: httpx.Request) -> httpx.Response:
            if request.url.path == "/robots.txt":
                return httpx.Response(200, text=ALLOW_ALL)
            return httpx.Response(403)

        provider = build(forbidden, site_pages_per_call=1)
        with pytest.raises(ProviderResponseInvalid, match="HTTP 403"):
            await provider.execute(history())


class TestConfiguration:
    def test_only_chains_someone_actually_verified_are_shipped(self) -> None:
        """A guessed module slug would 404 into a parse error on every call.
        A chain missing from the table is honest; a chain present on a guess
        is not. Polygon is the chain still missing: its slugs were never
        loaded."""
        assert set(DEFAULT_SITES) == {"ethereum", "tron"}
        assert DEFAULT_SITES["ethereum"].host == "3xpl.com"
        assert set(DEFAULT_SITES["ethereum"].modules) == {
            Capability.ADDRESS_HISTORY,
            Capability.TOKEN_TRANSFERS,
            Capability.INTERNAL_TRACES,
        }

    def test_tron_arrived_with_an_emitter_and_not_as_a_table_row(self) -> None:
        """This test used to assert Tron's ABSENCE, and the reason it gave was
        never the slugs — those were read off a live page on 2026-08-16. It was
        that this provider emitted Etherscan-shaped ROWS while `TronAdapter`
        reads a TronGrid-shaped BODY, so a "tron" entry alone would have routed
        Tron history here and handed the adapter a list where it calls
        `.get("data")`.

        Inverted rather than deleted, because the thing it guards is unchanged:
        Tron may be in this table only for as long as it names a dialect of its
        own. `rows` defaults to the Etherscan one, so dropping the field is
        exactly the one-line edit that was refused — and it is the edit that
        type-checks, passes every Ethereum test, and breaks only inside the
        adapter, at runtime, on a real trace."""
        tron = DEFAULT_SITES["tron"]
        assert tron.rows is not DEFAULT_SITES["ethereum"].rows
        assert tron.rows.envelope([], truncated=False) == {"data": []}

    def test_an_extra_site_is_a_table_edit(self) -> None:
        site = ExplorerSite(
            chain="testchain",
            base_url="https://example.test",
            path_chain="tc",
            native_asset_id="tc",
            native_decimals=9,
            modules={Capability.ADDRESS_HISTORY: "tc-main"},
        )
        provider = build(router({}), sites={"testchain": site})
        assert provider.supports("testchain", Capability.ADDRESS_HISTORY)
        assert not provider.supports("testchain", Capability.INTERNAL_TRACES)

    async def test_a_shared_robots_policy_is_consulted_by_the_provider(self) -> None:
        """The policy is injectable so several providers on one host share a
        single reading of its rules rather than each fetching robots again."""
        http = httpx.AsyncClient(
            transport=httpx.MockTransport(
                lambda request: httpx.Response(200, text="User-agent: *\nDisallow: /\n")
            )
        )
        policy = RobotsPolicy(http, user_agent="test-agent/1.0")
        provider = build(router({}), robots=policy)
        with pytest.raises(ProviderResponseInvalid, match="disallows"):
            await provider.execute(history())
