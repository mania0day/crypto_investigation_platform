"""Sources and parsers, exercised without touching a network or a database.

The split between ``load`` and ``parse`` exists so this file can exist: every
parser is a pure function of bytes, so a recorded document is a complete test.
Two of the three exchanges the harvester reads are unreachable from the host
this was written on, so for those it is the only option; Coinbase's page IS
fetchable, and its parser is still tested against a recorded copy, because a
test that needs the live site is a test that fails the day the site is slow.

What is pinned hardest is what each parser REFUSES. A harvest source feeds the
attributor, and the attributor is what puts an operator's name into a document
that goes to a regulating body.
"""

from __future__ import annotations

import shutil
from collections.abc import Mapping
from datetime import UTC, datetime
from pathlib import Path

import httpx
import pytest

from cipherchain.harvest.exchanges import (
    BINANCE,
    COINBASE,
    COINBASE_PARSERS,
    EXCHANGE_SPECS,
    OKX,
    daily_sources,
    manual_drop_sources,
)
from cipherchain.harvest.parsers import (
    PARSERS_BY_SUFFIX,
    parse_coinbase_reserves,
    parse_proof_of_reserves_csv,
)
from cipherchain.harvest.sources import (
    FirstAvailableSource,
    HarvestDocument,
    HttpDocumentSource,
    ManualDropSource,
    SourceRejected,
    SourceSpec,
    SourceUnavailable,
)
from cipherchain.intel.policy import TRUSTED_METHODS, IntelClaim, arrival_status

FIXTURES = Path(__file__).parent / "fixtures"
NOW = datetime(2026, 8, 16, 3, 15, tzinfo=UTC)

ALLOW_ALL_ROBOTS = "User-agent: *\nAllow: /\n"


def routed(pages: Mapping[str, httpx.Response]) -> httpx.AsyncClient:
    """A client that answers per path, so a test can say what robots.txt
    replied as well as what the document did. Anything unrouted is a 404, which
    for robots.txt is the documented allow-all."""

    def handler(request: httpx.Request) -> httpx.Response:
        response = pages.get(request.url.path)
        return response if response is not None else httpx.Response(404)

    return httpx.AsyncClient(transport=httpx.MockTransport(handler))


def drop(tmp_path: Path, spec: SourceSpec, fixture: str, date: str) -> Path:
    suffix = fixture.rsplit(".", 1)[-1]
    target = tmp_path / f"{spec.name}__{date}.{suffix}"
    shutil.copyfile(FIXTURES / fixture, target)
    return target


def source(tmp_path: Path, spec: SourceSpec) -> ManualDropSource:
    return ManualDropSource(spec, tmp_path, parsers=PARSERS_BY_SUFFIX)


async def claims_from(tmp_path: Path, spec: SourceSpec) -> list[IntelClaim]:
    unit = source(tmp_path, spec)
    return unit.parse(await unit.load(), retrieved_at=NOW)


class TestSourceSpec:
    def test_a_source_that_could_not_name_anybody_is_refused_at_construction(self) -> None:
        """An untrusted method arrives `pending`, so every row it produced
        would sit in the store naming nothing. `import_labelpacks.py` refuses
        the same thing for the same reason: silence there looks like success."""
        with pytest.raises(SourceRejected, match="not a trusted harvest tier"):
            SourceSpec(
                name="rumour-mill",
                entity="Someone",
                method="community",
                document_url="https://example.test/x",
            )

    def test_every_shipped_exchange_source_declares_a_trusted_method(self) -> None:
        for spec in EXCHANGE_SPECS:
            assert spec.method in TRUSTED_METHODS
            assert arrival_status(spec.method) == "active"

    def test_a_claim_is_never_certainty(self) -> None:
        with pytest.raises(SourceRejected, match="never proof"):
            SourceSpec(
                name="x",
                entity="X",
                method="first_party_published",
                document_url="https://example.test/x",
                confidence=1.0,
            )

    def test_the_three_exchanges_asked_for_are_the_three_shipped(self) -> None:
        assert [spec.entity for spec in EXCHANGE_SPECS] == ["Coinbase", "Binance", "OKX"]
        assert {COINBASE.name, BINANCE.name, OKX.name} == {spec.name for spec in EXCHANGE_SPECS}


class TestManualDrop:
    async def test_no_drop_says_what_file_it_was_looking_for(self) -> None:
        """A source with nothing to read is a normal cycle, not a defect — but
        the message has to be actionable, because "coverage quietly stopped
        refreshing" is this subsystem's real failure mode."""
        unit = ManualDropSource(BINANCE, Path("/nonexistent"), parsers=PARSERS_BY_SUFFIX)
        with pytest.raises(SourceUnavailable, match=r"binance-proof-of-reserves__<YYYY-MM-DD>"):
            await unit.load()

    async def test_the_newest_declared_date_wins(self, tmp_path: Path) -> None:
        drop(tmp_path, BINANCE, "binance_labelpack.json", "2026-07-01")
        drop(tmp_path, BINANCE, "binance_labelpack.json", "2026-08-14")
        document = await source(tmp_path, BINANCE).load()
        assert document.declared_date == datetime(2026, 8, 14, tzinfo=UTC)

    async def test_another_sources_drop_is_not_picked_up(self, tmp_path: Path) -> None:
        """Claim identity in the store is (chain, address, source). A file
        matched to the wrong source would land as that source's claim — and
        could then corroborate the real one, which is a source manufacturing
        its own corroborator."""
        drop(tmp_path, OKX, "okx_proof_of_reserves.csv", "2026-08-16")
        with pytest.raises(SourceUnavailable):
            await source(tmp_path, BINANCE).load()

    async def test_a_drop_without_a_date_in_its_name_is_not_a_drop(self, tmp_path: Path) -> None:
        shutil.copyfile(FIXTURES / "binance_labelpack.json", tmp_path / f"{BINANCE.name}.json")
        with pytest.raises(SourceUnavailable):
            await source(tmp_path, BINANCE).load()

    async def test_the_citation_is_the_published_url_not_the_local_path(
        self, tmp_path: Path
    ) -> None:
        drop(tmp_path, BINANCE, "binance_labelpack.json", "2026-08-14")
        claims = await claims_from(tmp_path, BINANCE)
        assert {claim.evidence_url for claim in claims} == {BINANCE.document_url}


class TestLabelpackParser:
    async def test_a_dropped_pack_becomes_dated_sourced_claims(self, tmp_path: Path) -> None:
        drop(tmp_path, BINANCE, "binance_labelpack.json", "2026-08-14")
        claims = await claims_from(tmp_path, BINANCE)
        assert len(claims) == 3
        assert {claim.source for claim in claims} == {"binance-proof-of-reserves"}
        assert {claim.method for claim in claims} == {"first_party_published"}
        assert {claim.source_date for claim in claims} == {datetime(2026, 8, 14, tzinfo=UTC)}
        assert {claim.chain for claim in claims} == {"ethereum", "bitcoin"}
        # The pack's own per-row confidence survives; the rest take the default.
        assert sorted(claim.confidence for claim in claims) == [0.8, 0.8, 0.85]

    async def test_a_pack_declaring_someone_elses_source_is_refused(self, tmp_path: Path) -> None:
        """Otherwise a Binance drop could carry a pack that files its rows as
        `okx-proof-of-reserves`, and `corroborates()` only requires a DIFFERENT
        source — one drop would then hold up its own claims."""
        target = drop(tmp_path, BINANCE, "binance_labelpack.json", "2026-08-14")
        target.write_text(
            target.read_text().replace(
                '"source": "binance-proof-of-reserves"', '"source": "okx-proof-of-reserves"'
            )
        )
        with pytest.raises(SourceRejected, match="another source's claims"):
            await claims_from(tmp_path, BINANCE)

    async def test_a_pack_claiming_a_stronger_method_than_its_source_is_refused(
        self, tmp_path: Path
    ) -> None:
        target = drop(tmp_path, BINANCE, "binance_labelpack.json", "2026-08-14")
        target.write_text(
            target.read_text().replace('"method": "first_party_published"', '"method": "signature"')
        )
        with pytest.raises(SourceRejected, match="declares method 'signature'"):
            await claims_from(tmp_path, BINANCE)

    async def test_a_pack_without_a_date_is_refused(self, tmp_path: Path) -> None:
        """A claim a reader cannot date is a claim a reader cannot weigh."""
        target = drop(tmp_path, BINANCE, "binance_labelpack.json", "2026-08-14")
        target.write_text(target.read_text().replace('"source_date": "2026-08-14",', ""))
        with pytest.raises(SourceRejected, match="no source_date"):
            await claims_from(tmp_path, BINANCE)

    async def test_a_pack_that_is_not_json_is_refused(self, tmp_path: Path) -> None:
        target = drop(tmp_path, BINANCE, "binance_labelpack.json", "2026-08-14")
        target.write_text("<html>login required</html>")
        with pytest.raises(SourceRejected, match="not valid JSON"):
            await claims_from(tmp_path, BINANCE)

    async def test_an_unreadable_confidence_is_refused_as_a_source_would(
        self, tmp_path: Path
    ) -> None:
        """Every way a document can be wrong has to come back as a HarvestError.
        A bare ValueError out of a parser is not a source failing — it is the
        cycle failing, and it takes the sources that already succeeded with it."""
        target = drop(tmp_path, BINANCE, "binance_labelpack.json", "2026-08-14")
        target.write_text(
            target.read_text().replace('"default_confidence": 0.8', '"default_confidence": "high"')
        )
        with pytest.raises(SourceRejected, match="not a number"):
            await claims_from(tmp_path, BINANCE)

    async def test_a_drop_dated_with_a_day_that_does_not_exist_is_refused(
        self, tmp_path: Path
    ) -> None:
        """`__2026-13-45` matches the file-name pattern — the pattern reads digit
        shapes, not calendars."""
        drop(tmp_path, BINANCE, "binance_labelpack.json", "2026-13-45")
        with pytest.raises(SourceRejected, match="not a date"):
            await source(tmp_path, BINANCE).load()


class TestProofOfReservesParser:
    async def test_the_summary_table_is_skipped_and_the_address_rows_are_read(
        self, tmp_path: Path
    ) -> None:
        """These files open with a per-coin balance table, so the first line is
        not the header — the real one is the row naming an address and the
        message signed over it."""
        drop(tmp_path, OKX, "okx_proof_of_reserves.csv", "2026-08-16")
        claims = await claims_from(tmp_path, OKX)
        assert [claim.chain for claim in claims] == [
            "ethereum",
            "ethereum",
            "bitcoin",
            "tron",
            "solana",
        ]
        assert {claim.entity for claim in claims} == {"OKX"}

    async def test_a_chain_cipherchain_cannot_trace_is_dropped_not_stored(
        self, tmp_path: Path
    ) -> None:
        """The TON row is a real address of a real exchange, and a label on a
        ledger nothing can traverse is a row that looks like coverage and is
        not."""
        drop(tmp_path, OKX, "okx_proof_of_reserves.csv", "2026-08-16")
        claims = await claims_from(tmp_path, OKX)
        assert not any(claim.address.startswith("UQ") for claim in claims)

    async def test_the_date_comes_from_the_drop_name_because_the_file_has_none(
        self, tmp_path: Path
    ) -> None:
        drop(tmp_path, OKX, "okx_proof_of_reserves.csv", "2026-08-16")
        claims = await claims_from(tmp_path, OKX)
        assert {claim.source_date for claim in claims} == {datetime(2026, 8, 16, tzinfo=UTC)}

    def test_it_refuses_to_record_the_signature_method_it_never_checked(self) -> None:
        """The signature tier means somebody verified that the key recovers to
        the address. Stamping it here — on a file whose signatures were never
        read — would upgrade an unverified document to the strongest provenance
        CipherChain has, on the strength of a file name."""
        spec = SourceSpec(
            name="okx-por",
            entity="OKX",
            method="signature",
            document_url="https://www.okx.com/proof-of-reserves",
        )
        document = HarvestDocument(
            raw=(FIXTURES / "okx_proof_of_reserves.csv").read_bytes(),
            origin=spec.document_url,
            declared_date=NOW,
            media="csv",
        )
        with pytest.raises(SourceRejected, match="does not check signatures"):
            parse_proof_of_reserves_csv(document, spec=spec, retrieved_at=NOW)

    def test_a_file_with_no_address_header_is_refused(self) -> None:
        document = HarvestDocument(
            raw=b"Coin,Balance\nBTC,1.0\n",
            origin=OKX.document_url,
            declared_date=NOW,
            media="csv",
        )
        with pytest.raises(SourceRejected, match="proof-of-reserves file"):
            parse_proof_of_reserves_csv(document, spec=OKX, retrieved_at=NOW)

    def test_a_file_that_yields_nothing_is_refused_rather_than_reported_empty(self) -> None:
        """A disclosure that parses to zero addresses is a format change, not an
        exchange that stopped holding funds. Refusing leaves the previous
        harvest's rows standing instead of letting coverage age out quietly."""
        document = HarvestDocument(
            raw=b"Network,Address,Message\nTON,UQabc,I am an OKX address\n",
            origin=OKX.document_url,
            declared_date=NOW,
            media="csv",
        )
        with pytest.raises(SourceRejected, match="no usable rows"):
            parse_proof_of_reserves_csv(document, spec=OKX, retrieved_at=NOW)


class TestCoinbaseReservesParser:
    """The one exchange disclosure this host can read for itself.

    `coinbase_cbbtc_reserves.html` is a real recording, pruned: the page's own
    `server-app-state` island, the same Relay `recordMap` shape, three of the
    52 Bitcoin reserve addresses it published on 2026-08-16, and one address on
    a ledger CipherChain has no adapter for.
    """

    RECORDED = FIXTURES / "coinbase_cbbtc_reserves.html"

    def document(self, raw: bytes | None = None) -> HarvestDocument:
        return HarvestDocument(
            raw=self.RECORDED.read_bytes() if raw is None else raw,
            origin=COINBASE.document_url,
            declared_date=None,
            media="html",
        )

    def parse(self, raw: bytes | None = None, spec: SourceSpec = COINBASE) -> list[IntelClaim]:
        return parse_coinbase_reserves(self.document(raw), spec=spec, retrieved_at=NOW)

    def test_the_published_reserve_addresses_become_dated_sourced_claims(self) -> None:
        claims = self.parse()
        assert [claim.address for claim in claims] == [
            "bc1qfszruqaal85d88qvgx25e2ttq7zf6ze6kpc5h7",
            "bc1q9e83uh7etgzmfrmjd389p7yjv5etsa6k3dq808",
            "bc1qsefydr2r5ysjep7z4zelvz0ulmj29vlvarraqg",
        ]
        assert {claim.entity for claim in claims} == {"Coinbase"}
        assert {claim.chain for claim in claims} == {"bitcoin"}
        assert {claim.method for claim in claims} == {"first_party_published"}
        assert {claim.evidence_url for claim in claims} == {COINBASE.document_url}

    def test_the_page_dates_itself_so_no_operator_declaration_is_needed(self) -> None:
        """This is what makes Coinbase automatable at all. A proof-of-reserves
        CSV has to borrow its date from the drop's file name; the reserves page
        states `lastUpdatedAt`, so nothing about the claim depends on a human
        naming a file correctly."""
        assert {claim.source_date for claim in self.parse()} == {datetime(2026, 8, 16, tzinfo=UTC)}

    def test_the_recomputed_hour_is_dropped_so_two_runs_a_day_stay_idempotent(self) -> None:
        """The recorded page says 16:32:11; a fetch fifteen minutes later said
        16:47:11, over an identical address list. `source_date` is part of a
        claim's identity, so keeping the second would have made every cycle
        report 52 rows `updated` about a document that had not changed — and
        would have made two runs on one day disagree, which the cycle is
        documented not to do."""
        moved = self.RECORDED.read_text(encoding="utf-8").replace(
            "2026-08-16T16:32:11Z", "2026-08-16T23:59:59Z"
        )
        assert {claim.source_date for claim in self.parse(moved.encode("utf-8"))} == {
            datetime(2026, 8, 16, tzinfo=UTC)
        }

    def test_a_ledger_cipherchain_cannot_trace_is_dropped_not_stored(self) -> None:
        """Coinbase publishes the same page for cbADA, cbXRP, cbDOGE and cbLTC.
        A label on a chain nothing can traverse is a row that looks like
        coverage and provides none."""
        assert not any(claim.address.startswith("addr1") for claim in self.parse())

    def test_a_page_without_the_state_block_is_refused(self) -> None:
        """The bot-check interstitial case, and the redesign case. Both come
        back as HTML that is not this page, and neither may parse to zero
        addresses quietly."""
        with pytest.raises(SourceRejected, match="server-app-state"):
            self.parse(b"<html><body>Please enable JavaScript</body></html>")

    def test_a_state_block_that_is_not_the_relay_store_is_refused(self) -> None:
        with pytest.raises(SourceRejected, match="relayStoreData"):
            self.parse(
                b'<script id="server-app-state" type="application/json">{"isLoggedIn":false}'
                b"</script>"
            )

    def test_a_page_that_yields_no_traceable_address_is_refused_not_reported_empty(
        self,
    ) -> None:
        """Zero rows would read as "Coinbase holds no reserves". Refusing leaves
        yesterday's rows standing and puts a line in the cron mail; a false
        empty ages the coverage out in silence."""
        text = self.RECORDED.read_text(encoding="utf-8").replace("bitcoin", "cardano")
        with pytest.raises(SourceRejected, match="no address on a chain CipherChain can trace"):
            self.parse(text.encode("utf-8"))

    def test_a_page_that_states_no_publication_date_is_refused(self) -> None:
        text = self.RECORDED.read_text(encoding="utf-8").replace("lastUpdatedAt", "lastTouchedAt")
        with pytest.raises(SourceRejected, match="lastUpdatedAt"):
            self.parse(text.encode("utf-8"))

    def test_it_refuses_to_record_the_signature_method_it_never_checked(self) -> None:
        """The reserves page publishes addresses, not proofs. Recording the
        signature tier off it would give CipherChain's strongest provenance to a
        document in which nobody signed anything."""
        spec = SourceSpec(
            name="coinbase-signed",
            entity="Coinbase",
            method="signature",
            document_url=COINBASE.document_url,
        )
        with pytest.raises(SourceRejected, match="does not check signatures"):
            self.parse(spec=spec)


class TestHttpSource:
    async def test_robots_is_read_before_the_document_and_a_disallow_declines(self) -> None:
        """The fetch tier's boundary, held here too. A disallow is a correct
        outcome, and in this package it has a second use: the source falls
        through to its drop path, so the publisher gets read by a human rather
        than by us."""
        http = routed(
            {
                "/robots.txt": httpx.Response(200, text="User-agent: *\nDisallow: /cbbtc/\n"),
                "/cbbtc/proof-of-reserves": httpx.Response(200, content=b"should never be read"),
            }
        )
        unit = HttpDocumentSource(COINBASE, http, parser=parse_coinbase_reserves, media="html")
        with pytest.raises(SourceUnavailable, match=r"robots\.txt.*disallows"):
            await unit.load()

    async def test_robots_that_cannot_be_read_fails_closed(self) -> None:
        """ "We could not read the rules" must never resolve to "so we fetched
        anyway" — RFC 9309 §2.3.1.4, and the reason `RobotsPolicy` exists."""
        http = routed({"/robots.txt": httpx.Response(503)})
        unit = HttpDocumentSource(COINBASE, http, parser=parse_coinbase_reserves, media="html")
        with pytest.raises(SourceUnavailable, match="rules unknown"):
            await unit.load()

    async def test_a_moved_document_is_reported_not_followed(self) -> None:
        """The bug this whole change came out of: the configured Coinbase url
        404'd for an unknown length of time and the cycle said nothing worse
        than "unavailable". A url that has moved needs a person to read what
        the new page publishes before anything is harvested off it."""
        http = routed(
            {
                "/robots.txt": httpx.Response(200, text=ALLOW_ALL_ROBOTS),
                "/cbbtc/proof-of-reserves": httpx.Response(
                    301, headers={"Location": "https://www.coinbase.com/somewhere-else"}
                ),
            }
        )
        unit = HttpDocumentSource(COINBASE, http, parser=parse_coinbase_reserves, media="html")
        with pytest.raises(SourceUnavailable, match="has moved"):
            await unit.load()

    async def test_a_publisher_that_is_down_is_unavailable_not_a_crash(self) -> None:
        """The primary path wherever the network allows it. One unreachable
        publisher slows the cycle down; it does not end it. HTTP 202 with an
        empty body is Binance's live answer to a script."""
        http = routed(
            {
                "/robots.txt": httpx.Response(200, text=ALLOW_ALL_ROBOTS),
                "/en/proof-of-reserves": httpx.Response(202, text=""),
            }
        )
        unit = HttpDocumentSource(BINANCE, http, parser=PARSERS_BY_SUFFIX["json"])
        with pytest.raises(SourceUnavailable, match="answered HTTP 202"):
            await unit.load()

    async def test_a_transport_failure_is_unavailable_too(self) -> None:
        def handler(request: httpx.Request) -> httpx.Response:
            raise httpx.ConnectError("no route to host")

        http = httpx.AsyncClient(transport=httpx.MockTransport(handler))
        unit = HttpDocumentSource(OKX, http, parser=PARSERS_BY_SUFFIX["json"])
        # robots is unreachable before the document is, and that already
        # declines — the fail-closed path, reached without a single page fetch.
        with pytest.raises(SourceUnavailable, match=r"robots\.txt unreachable"):
            await unit.load()

    async def test_a_reachable_publisher_is_parsed_with_no_drop_involved(self) -> None:
        http = routed(
            {
                "/robots.txt": httpx.Response(200, text=ALLOW_ALL_ROBOTS),
                "/cbbtc/proof-of-reserves": httpx.Response(
                    200, content=(FIXTURES / "coinbase_cbbtc_reserves.html").read_bytes()
                ),
            }
        )
        unit = HttpDocumentSource(COINBASE, http, parser=parse_coinbase_reserves, media="html")
        document = await unit.load()
        assert document.media == "html"  # travels with the bytes, for parse dispatch
        assert document.origin == COINBASE.document_url
        assert len(unit.parse(document, retrieved_at=NOW)) == 3


class TestFirstAvailable:
    """One publisher, two transports, one source identity.

    Splitting the fetched copy and the hand-saved copy into two sources would
    give a store keyed on `(chain, address, source)` two Coinbase rows per
    address — and `corroborates()` only asks for a DIFFERENT source, so the two
    copies of one page would hold each other up.
    """

    def coinbase(self, http: httpx.AsyncClient, drop_dir: Path) -> FirstAvailableSource:
        return FirstAvailableSource(
            COINBASE,
            [
                HttpDocumentSource(COINBASE, http, parser=parse_coinbase_reserves, media="html"),
                ManualDropSource(COINBASE, drop_dir, parsers=COINBASE_PARSERS),
            ],
            parsers=COINBASE_PARSERS,
        )

    def serving_the_page(self) -> httpx.AsyncClient:
        return routed(
            {
                "/robots.txt": httpx.Response(200, text=ALLOW_ALL_ROBOTS),
                "/cbbtc/proof-of-reserves": httpx.Response(
                    200, content=(FIXTURES / "coinbase_cbbtc_reserves.html").read_bytes()
                ),
            }
        )

    async def test_the_fetch_wins_when_the_host_can_reach_the_publisher(
        self, tmp_path: Path
    ) -> None:
        drop(tmp_path, COINBASE, "binance_labelpack.json", "2026-01-01")
        unit = self.coinbase(self.serving_the_page(), tmp_path)
        document = await unit.load()
        assert document.media == "html"
        assert len(unit.parse(document, retrieved_at=NOW)) == 3

    async def test_a_host_with_no_route_falls_through_to_the_operators_drop(
        self, tmp_path: Path
    ) -> None:
        """The whole point of the fallback: losing the route slows the source
        down to a human's pace instead of removing it."""
        shutil.copyfile(
            FIXTURES / "coinbase_cbbtc_reserves.html",
            tmp_path / f"{COINBASE.name}__2026-08-16.html",
        )
        unit = self.coinbase(routed({"/robots.txt": httpx.Response(503)}), tmp_path)
        document = await unit.load()
        assert document.media == "html"
        # The citation is still the published url, not the operator's disk.
        assert document.origin == COINBASE.document_url
        assert len(unit.parse(document, retrieved_at=NOW)) == 3

    async def test_the_reading_follows_the_bytes_not_the_usual_transport(
        self, tmp_path: Path
    ) -> None:
        """A labelpack dropped for Coinbase is read as a labelpack even though
        this source normally receives HTML. Dispatch is on the document's own
        media, so a fallback cannot be read with the primary path's parser."""
        target = drop(tmp_path, COINBASE, "binance_labelpack.json", "2026-08-14")
        target.write_text(
            target.read_text().replace(BINANCE.name, COINBASE.name).replace("Binance", "Coinbase")
        )
        unit = self.coinbase(routed({"/robots.txt": httpx.Response(503)}), tmp_path)
        document = await unit.load()
        assert document.media == "json"
        claims = unit.parse(document, retrieved_at=NOW)
        assert {claim.source for claim in claims} == {COINBASE.name}
        assert all(claim.entity.startswith("Coinbase") for claim in claims)

    async def test_a_document_that_arrived_and_is_wrong_is_not_papered_over(
        self, tmp_path: Path
    ) -> None:
        """SourceRejected stops the source. Falling through to the drop would
        hide "the publisher's page changed shape" behind whatever an operator
        happened to leave on disk months ago — which is the exact substitution
        this subsystem must never make silently."""
        shutil.copyfile(
            FIXTURES / "coinbase_cbbtc_reserves.html",
            tmp_path / f"{COINBASE.name}__2026-08-16.html",
        )
        http = routed(
            {
                "/robots.txt": httpx.Response(200, text=ALLOW_ALL_ROBOTS),
                "/cbbtc/proof-of-reserves": httpx.Response(200, content=b"<html>nope</html>"),
            }
        )
        unit = self.coinbase(http, tmp_path)
        document = await unit.load()
        with pytest.raises(SourceRejected, match="server-app-state"):
            unit.parse(document, retrieved_at=NOW)

    async def test_when_nothing_works_every_reason_is_named(self, tmp_path: Path) -> None:
        """ "Coinbase contributed nothing" is not actionable. "Robots declined
        and there is no drop either" says which of the two to go and do."""
        unit = self.coinbase(routed({"/robots.txt": httpx.Response(503)}), tmp_path)
        with pytest.raises(SourceUnavailable) as raised:
            await unit.load()
        assert "rules unknown" in str(raised.value)
        assert "no drop in" in str(raised.value)


class TestWiring:
    def test_the_shipped_sources_all_read_from_one_drop_directory(self, tmp_path: Path) -> None:
        """Returned even when the directory is empty: "no drop this cycle" and
        "this source was never configured" must not look the same."""
        sources = manual_drop_sources(tmp_path)
        assert [unit.spec.name for unit in sources] == [spec.name for spec in EXCHANGE_SPECS]

    def test_the_daily_cycle_fetches_coinbase_and_waits_on_the_other_two(
        self, tmp_path: Path
    ) -> None:
        """The answer to "does it update daily": one of the three does. Binance
        and OKX get no HTTP loader at all, because pointing one at a site that
        has already said no just adds a request that fails every morning."""
        sources = daily_sources(tmp_path, httpx.AsyncClient())
        by_name = {unit.spec.name: unit for unit in sources}
        assert isinstance(by_name[COINBASE.name], FirstAvailableSource)
        assert isinstance(by_name[BINANCE.name], ManualDropSource)
        assert isinstance(by_name[OKX.name], ManualDropSource)

    def test_the_coinbase_url_that_404d_does_not_come_back(self) -> None:
        """`https://www.coinbase.com/legal/transparency` was configured, was
        never reachable, and made the source look merely unavailable. A url
        known to 404 is worse than no url: it reads as a source that exists."""
        assert COINBASE.document_url == "https://www.coinbase.com/cbbtc/proof-of-reserves"
        assert "legal/transparency" not in COINBASE.document_url

    def test_the_page_that_restates_itself_hourly_gets_a_tight_staleness_window(self) -> None:
        """Cadence is a property of the publisher. Coinbase's `lastUpdatedAt`
        moved within the same afternoon it was measured; a proof-of-reserves
        file is monthly. One threshold for both would either cry every fourth
        week or never fire at all."""
        assert COINBASE.stale_after_days == 3
        assert BINANCE.stale_after_days >= 31
        assert OKX.stale_after_days >= 31

    def test_a_source_that_is_stale_the_moment_it_succeeds_is_refused(self) -> None:
        with pytest.raises(SourceRejected, match="at least 1 day"):
            SourceSpec(
                name="x",
                entity="X",
                method="first_party_published",
                document_url="https://example.test/x",
                stale_after_days=0,
            )
