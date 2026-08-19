"""The OFAC SDN source, proved against a recorded document and never a live one.

The test that matters most here is not that a good document parses. It is
:class:`TestTruncation`: a partial download of this list still parses far
enough to look like a working day, and if it is ingested the addresses past the
cut quietly stop being sanctioned. A trace through one of them then reports
"no named endpoint", which reads as *the money went nowhere interesting* rather
than *the store is missing half a sanctions list*. There is no error anywhere
in that sequence, which is why it has to be pinned by test rather than noticed
in a report. :class:`TestMalformedButComplete` is its other half: the same
exception class comes out of ``ElementTree`` for a document that arrived whole
and is broken, and the operator instruction for that one is the opposite —
re-running a cut-off download is the fix, re-running a malformed document is a
cron job failing at 03:15 every morning with nobody reading the file.

Everything runs off ``fixtures/ofac_sdn.xml``, cut from the real 28.8 MB
document published 2026-08-18 (see ``fixtures/README.md``). The live list is
fetchable from this host — that is the point of this whole source — and is
still not fetched here, because a test that needs treasury.gov is a test that
fails on the day treasury.gov is slow, and because a recording is the only way
to pin what this parser does when the document changes shape.
"""

from __future__ import annotations

import logging
import shutil
from collections.abc import Mapping
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import httpx
import pytest
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from cipherchain.harvest.exchanges import daily_sources
from cipherchain.harvest.parsers import SDN_LEDGER_CHAINS, parse_ofac_sdn
from cipherchain.harvest.sanctions import (
    OFAC_PARSERS,
    OFAC_SDN,
    OFAC_SDN_TIMEOUT_SECONDS,
    OFAC_SDN_URL,
    OFAC_STORAGE_HOSTS,
    ofac_sdn_source,
)
from cipherchain.harvest.sources import (
    FirstAvailableSource,
    HarvestDocument,
    HttpDocumentSource,
    SourceRejected,
    SourceUnavailable,
)
from cipherchain.harvest.worker import HarvestWorker
from cipherchain.intel.policy import IntelClaim, arrival_status, entity_stem
from cipherchain.storage.repositories import LabelRepository

FIXTURES = Path(__file__).parent / "fixtures"
SDN = FIXTURES / "ofac_sdn.xml"
NOW = datetime(2026, 8, 19, 3, 15, tzinfo=UTC)

# The list's own Publish_Date, verbatim from the recording. Every claim is dated
# from this and not from NOW: re-reading a June document at 03:15 this morning
# says nothing about June.
PUBLISHED = datetime(2026, 8, 18, tzinfo=UTC)

# A signed handoff url of the shape the endpoint really answers with — the host
# from OFAC's GovCloud bucket, and the one-hour expiry that is the reason
# `origin` on the document must stay the configured url.
SIGNED_URL = (
    f"https://{next(iter(OFAC_STORAGE_HOSTS))}/Published/02b5631d/2026-08-18/SDN.XML"
    "?X-Amz-Expires=3600&X-Amz-Signature=deadbeef"
)

# Addresses OFAC publishes in this recording on currencies CipherChain does not
# model. Not one of them may appear on any claim, on any chain.
UNMODELLED_ADDRESSES = (
    "5be5543ff73456ab9f2d207887e2af87322c651ea1a873c5b25b7ffae456c320",  # XMR
    "LNwgtMxcKUQ51dw7bQL1yPQjBVZh6QEqsd",  # LTC
    "t1g7wowvQ8gn2v8jrU1biyJ26sieNqNsBJy",  # ZEC
    "XnPFsRWTaSgiVauosEwQ6dEitGYXgwznz2",  # DASH
    "GPwg61XoHqQPNmAucFACuQ5H9sGCDv9TpS",  # BTG
    "bnb136ns6lfw4zs5hg4n85vdthaad7hq5m4gtkgf23",  # BNB
)


# The one row in the live document whose ticker and address contradict each
# other. Of the 977 digital-currency rows published 2026-08-18 exactly one does,
# and both values here are verbatim from it (id uid 165924, on the entry for
# "Mingming WANG", ILLICIT-DRUGS-EO14059): OFAC's Bitcoin ticker carrying `T`
# plus 33 base58 characters, which is a Tron account and can be nothing else.
# The recording keeps 8 of that document's 19,202 entries and Mingming WANG is
# not among them, so the row is spliced into an entry that is — the pair
# (idType, idNumber) is what is under test, not which party it hangs off.
MISFILED_ADDRESS = "TUCsTq7TofTCJRRoHk6RvhMoS2mJLm5Yzq"
MISFILED_ROW = b"""      <id>
        <uid>165924</uid>
        <idType>Digital Currency Address - XBT</idType>
        <idNumber>TUCsTq7TofTCJRRoHk6RvhMoS2mJLm5Yzq</idNumber>
      </id>
"""
_SPLICE_POINT = b"      <id>\n        <uid>170746</uid>"

# A real Bitcoin address out of the recording, reused below as a SOL row's
# idNumber. Base58 with a `1` prefix is a Bitcoin address AND a possible Solana
# account, which is the shape the parser must NOT treat as a contradiction.
BITCOIN_SHAPED = "13fhnkmpBBWXUQucJd6efWvXdEj78DKavk"

# The parser's own logger, so a test can read the count it reports rather than
# trusting that a mismatch was noticed.
PARSER_LOG = "cipherchain.harvest.parsers"


def document(raw: bytes) -> HarvestDocument:
    return HarvestDocument(raw=raw, origin=OFAC_SDN_URL, declared_date=None, media="xml")


def with_misfiled_row() -> bytes:
    """The recording with that contradicting row added to the first entry."""
    raw = SDN.read_bytes()
    assert raw.count(_SPLICE_POINT) == 1  # the splice landed where it was meant to
    return raw.replace(_SPLICE_POINT, MISFILED_ROW + _SPLICE_POINT)


def with_doctype(entities: bytes, reference: bytes) -> bytes:
    """The recording plus a DTD, referenced once from the first entry's name.

    Complete documents, every one of them: the root opens and closes and the
    last byte is where the publisher meant the file to end. What is wrong with
    them is inside, which is the whole distinction being tested.
    """
    raw = SDN.read_bytes()
    with_dtd = raw.replace(b"<sdnList ", b"<!DOCTYPE sdnList [" + entities + b"]>\n<sdnList ", 1)
    assert with_dtd != raw
    spliced = with_dtd.replace(
        b"<lastName>OKO DESIGN BUREAU</lastName>",
        b"<lastName>" + reference + b"</lastName>",
        1,
    )
    assert spliced != with_dtd
    assert spliced.rstrip().endswith(b"</sdnList>")
    return spliced


def claims(raw: bytes | None = None) -> list[IntelClaim]:
    return parse_ofac_sdn(
        document(SDN.read_bytes() if raw is None else raw), spec=OFAC_SDN, retrieved_at=NOW
    )


def routed(pages: Mapping[str, httpx.Response]) -> httpx.AsyncClient:
    """Answers on the FULL url, because the interesting case here spans two
    hosts: the publisher's endpoint and the bucket it hands off to. Anything
    unrouted is a 404 — which for ``robots.txt`` is the documented allow-all,
    and is also what treasury.gov really answers."""

    def handler(request: httpx.Request) -> httpx.Response:
        return pages.get(str(request.url), httpx.Response(404))

    return httpx.AsyncClient(transport=httpx.MockTransport(handler))


def unreachable() -> httpx.AsyncClient:
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("no route to host")

    return httpx.AsyncClient(transport=httpx.MockTransport(handler))


def serving(body: bytes) -> httpx.AsyncClient:
    """The live shape: the endpoint 302s onto a signed bucket url that serves
    the document."""
    return routed(
        {
            OFAC_SDN_URL: httpx.Response(302, headers={"Location": SIGNED_URL}),
            SIGNED_URL: httpx.Response(200, content=body),
        }
    )


def http_source(http: httpx.AsyncClient) -> HttpDocumentSource:
    """The fetch half of the shipped source, on its own, so a test can see the
    reason instead of the fallback's summary of it."""
    source = ofac_sdn_source(Path("/nonexistent"), http)
    assert isinstance(source, FirstAvailableSource)
    # Reaching into the assembled source on purpose: what is under test is the
    # loader `ofac_sdn_source` really ships, not one a test rebuilt to match.
    loader = source._loaders[0]
    assert isinstance(loader, HttpDocumentSource)
    return loader


class TestCompleteDocument:
    def test_every_address_on_a_chain_this_project_models_becomes_a_claim(self) -> None:
        """Eight entries, 39 digital-currency rows, 30 claims. The gap is the
        point of the other tests in this file: nine rows are on currencies
        CipherChain does not model or are the same address listed twice."""
        by_chain: dict[str, int] = {}
        for claim in claims():
            by_chain[claim.chain] = by_chain.get(claim.chain, 0) + 1
        assert by_chain == {"bitcoin": 9, "ethereum": 13, "tron": 7, "solana": 1}

    def test_each_address_lands_on_the_ledger_its_encoding_states(self) -> None:
        chains = {claim.address: claim.chain for claim in claims()}
        assert chains["13fhnkmpBBWXUQucJd6efWvXdEj78DKavk"] == "bitcoin"
        assert chains["bc1qardcxz845jw83vgfvcmewhuxa0ag9gchjwmcfd"] == "bitcoin"
        assert chains["0x19F8f2B0915Daa12a3f5C9CF01dF9E24D53794F7"] == "ethereum"
        assert chains["TFdTr9C3BqQrzKBXqSxJfAZFTh8UwBAfSg"] == "tron"
        assert chains["6wjqWWra8ombzaw6VHrG5xpQ972jCYF6bbHiFCbWmr4U"] == "solana"

    def test_usdt_is_resolved_from_the_address_because_the_ticker_cannot_say(self) -> None:
        """`Digital Currency Address - USDT` is published for three different
        ledgers in this one recording — an Ethereum contract holder, a Tron
        account, and Garantex's Omni-layer address, which is a plain Bitcoin
        address. Mapping the TICKER to a chain would have put the last of those
        on the wrong ledger or dropped it; both lose a sanctioned address."""
        chains = {claim.address: claim.chain for claim in claims()}
        assert chains["0x983a81ca6FB1e441266D2FbcB7D8E530AC2E05A2"] == "ethereum"
        assert chains["TVacWx7F5wgMgn49L5frDf9KLgdYy8nPHL"] == "tron"
        assert chains["3E6ZCKRrsdPc35chA9Eftp1h3DLW18NFNV"] == "bitcoin"

    def test_the_claim_names_the_designated_party_and_the_programs(self) -> None:
        """What an investigator needs off a sanctions hit is WHO and under what
        authority. The programs sit in the parenthetical because that is this
        project's annotation syntax (`policy.entity_stem` strips it), so
        corroboration still matches on the party rather than on whichever
        programme list was current that week."""
        entities = {claim.address: claim.entity for claim in claims()}
        assert (
            entities["13fhnkmpBBWXUQucJd6efWvXdEj78DKavk"] == "OKO DESIGN BUREAU (RUSSIA-EO14024)"
        )
        assert (
            entities["3E6ZCKRrsdPc35chA9Eftp1h3DLW18NFNV"]
            == "GARANTEX EUROPE OU (CYBER4, RUSSIA-EO14024)"
        )
        assert entities["1Q6saNmqKkyFB9mFR68Ck8F7Dp7dTopF2W"] == "Dmitrii KARASAVIDI (CYBER2)"

    def test_the_entry_is_named_after_itself_and_never_after_one_of_its_akas(self) -> None:
        """This entry carries `SHELBIT EXCHANGE` in its `akaList`, and an
        `akaList` is full of the same `lastName` element the entry uses. A
        descendant search would name the address after an alias — a different
        string, which stems differently, which silently stops corroborating the
        real party."""
        entities = {claim.address: claim.entity for claim in claims()}
        assert entities["6wjqWWra8ombzaw6VHrG5xpQ972jCYF6bbHiFCbWmr4U"] == "SHPS SHELBIT (SDGT)"

    def test_one_address_listed_under_several_tickers_is_one_claim(self) -> None:
        """This address is published three times in one entry — under USDT,
        USDC and ETH. The store keys on (chain, address, source), so emitting
        all three would have them overwrite each other every cycle."""
        addresses = [claim.address for claim in claims()]
        assert addresses.count("0x983a81ca6FB1e441266D2FbcB7D8E530AC2E05A2") == 1

    def test_the_list_dates_itself_and_cites_the_published_url(self) -> None:
        """`Publish_Date`, not the day of the fetch — that value is what the
        staleness alarm reads, and it is the only thing that can tell a list
        republished this morning from one nobody has touched since spring."""
        claim = claims()[0]
        assert claim.source_date == PUBLISHED
        assert claim.evidence_url == OFAC_SDN_URL

    def test_a_sanctions_claim_arrives_active_and_categorised_as_sanctions(self) -> None:
        """`sanctioned`, not the spec default of `vasp`: `corroborates()`
        requires a matching category, so a listing filed as `vasp` could
        confirm a claim that the address is an exchange — a different assertion
        about the same address, shown in the store as agreement."""
        claim = claims()[0]
        assert (claim.category, claim.role) == ("sanctioned", "unknown")
        assert claim.source == "ofac-sdn"
        assert arrival_status(claim.method) == "active"

    def test_an_entry_with_no_digital_currency_rows_contributes_nothing(self) -> None:
        """Most of the 19,202 entries are people and companies with no crypto
        at all, and one of them is in this recording carrying an
        `Executive Order 13846 information:` id row. Prose ids must not be read
        as currencies — the ticker pattern is anchored at both ends for exactly
        this."""
        assert not [claim for claim in claims() if "GALAXY" in claim.entity]


class TestTruncation:
    """A partial download must be refused, not ingested as a short list.

    Measured on the real document: a 200-second ceiling returned 15,388,672 of
    28,812,494 bytes — 9,959 complete entries and 564 of the 977
    digital-currency rows. Every address after the cut would have silently
    stopped being sanctioned, with nothing anywhere reporting a failure.
    """

    def test_a_document_cut_part_way_is_refused_although_it_parses_far_enough(self) -> None:
        cut = SDN.read_bytes()[:9000]
        # The premise of the whole guard: the prefix is not junk. It holds
        # complete entries with real sanctioned addresses in them, and a parser
        # that emitted as it went would have had claims in hand.
        assert cut.count(b"</sdnEntry>") >= 2
        assert b"Digital Currency Address" in cut

        with pytest.raises(SourceUnavailable, match="stops part way through"):
            claims(cut)

    def test_a_document_missing_only_its_closing_element_is_refused(self) -> None:
        """The meanest cut of all: every entry in the body is complete and
        well-formed, and the only thing absent is the end of the list. This is
        what a connection dropped on the last chunk looks like, and it is
        indistinguishable from a full document until the parser insists on
        seeing the root close."""
        whole = SDN.read_bytes()
        assert whole.endswith(b"</sdnList>")
        with pytest.raises(SourceUnavailable, match="stops part way through"):
            claims(whole[: -len(b"</sdnList>")])

    def test_the_refusal_says_how_much_arrived_so_the_cause_is_visible(self) -> None:
        """ "Unavailable" alone would read as "OFAC published nothing today".
        The byte count and the entry count are what tell an operator that the
        document IS there and the fetch is being cut off."""
        with pytest.raises(SourceUnavailable) as raised:
            claims(SDN.read_bytes()[:9000])
        assert "9000 bytes" in str(raised.value)
        assert "silently stop being sanctioned" in str(raised.value)

    async def test_a_truncated_fetch_produces_no_claims_at_all(self) -> None:
        """End to end through the shipped source: the bytes arrive, the fetch
        reports success, and the parse still refuses. Nothing partial reaches
        the caller — the fail-closed decision is inside the parser, where every
        transport has to pass through it."""
        source = ofac_sdn_source(Path("/nonexistent"), serving(SDN.read_bytes()[:9000]))
        loaded = await source.load()
        assert len(loaded.raw) == 9000  # the transport is perfectly happy
        with pytest.raises(SourceUnavailable):
            source.parse(loaded, retrieved_at=NOW)

    def test_a_document_that_is_not_the_sdn_list_is_rejected_not_merely_unavailable(self) -> None:
        """Different failure, different operator response: unavailable means
        wait for tomorrow, rejected means go and look at what the publisher is
        serving."""
        with pytest.raises(SourceRejected, match="not <sdnList>"):
            claims(b"<html><body>Access denied</body></html>")


class TestMalformedButComplete:
    """A body that arrived whole and is broken is not a cut-off download.

    Both come out of ``ElementTree`` as one exception class, and reporting both
    as "the document stops part way through" sends the operator to the wrong
    job: ``scripts/harvest.sh`` reads that line as *the download was cut off,
    re-run it*, and re-running a malformed document fetches the same bytes and
    fails identically tomorrow morning, and the morning after. So the two are
    told apart, and each says what to do — wait (:class:`SourceUnavailable`)
    against go and look at the document (:class:`SourceRejected`), which is the
    distinction those two classes are documented to carry.
    """

    def test_junk_after_the_document_element_is_not_reported_as_a_short_download(self) -> None:
        """The clearest case: every byte the publisher meant to send arrived,
        the root opened and closed, and something appended more after it. A
        truncated document is the one thing this cannot be."""
        appended = SDN.read_bytes() + b"\n<sdnList/>\n"
        with pytest.raises(SourceRejected) as raised:
            claims(appended)
        assert "stops part way through" not in str(raised.value)
        assert "did NOT stop mid-stream" in str(raised.value)

    def test_the_malformed_refusal_tells_the_operator_that_retrying_is_pointless(self) -> None:
        """The operator instruction is the defect being fixed. "Re-run it" on a
        malformed body is a cron job that fails at 03:15 every morning with
        nobody looking at the file."""
        with pytest.raises(SourceRejected) as raised:
            claims(SDN.read_bytes() + b"\n<sdnList/>\n")
        assert "fails identically" in str(raised.value)

    def test_an_external_entity_is_refused_and_nothing_is_read_off_this_host(self) -> None:
        """A complete document carrying an XXE reference. It is refused as
        malformed — and refused by ElementTree declining to resolve the entity
        at all, so no local file is opened and no local file can reach a claim
        or an error message."""
        with pytest.raises(SourceRejected) as raised:
            claims(with_doctype(b'<!ENTITY passwd SYSTEM "file:///etc/passwd">', b"&passwd;"))
        assert "undefined entity" in str(raised.value)
        assert "did NOT stop mid-stream" in str(raised.value)
        assert "root:" not in str(raised.value)

    def test_an_entity_bomb_is_refused_by_the_parsers_own_limit(self) -> None:
        """Nested entities that would expand to hundreds of megabytes. expat
        breaches its amplification limit and raises rather than expanding them,
        so this lands as one more malformed document — complete, broken, and
        not worth retrying."""
        nested = (
            b'<!ENTITY a "' + b"a" * 100 + b'">'
            b'<!ENTITY b "&a;&a;&a;&a;&a;&a;&a;&a;&a;&a;">'
            b'<!ENTITY c "&b;&b;&b;&b;&b;&b;&b;&b;&b;&b;">'
            b'<!ENTITY d "&c;&c;&c;&c;&c;&c;&c;&c;&c;&c;">'
            b'<!ENTITY e "&d;&d;&d;&d;&d;&d;&d;&d;&d;&d;">'
            b'<!ENTITY f "&e;&e;&e;&e;&e;&e;&e;&e;&e;&e;">'
            b'<!ENTITY g "&f;&f;&f;&f;&f;&f;&f;&f;&f;&f;">'
        )
        with pytest.raises(SourceRejected) as raised:
            claims(with_doctype(nested, b"&g;"))
        assert "amplification" in str(raised.value)

    def test_a_cut_off_download_is_still_the_other_answer(self) -> None:
        """The pair. Same parser, same exception class out of ElementTree, and
        the two documents must not converge on one message: this one IS worth
        retrying, and its rows are not to be looked at by anybody."""
        with pytest.raises(SourceUnavailable) as raised:
            claims(SDN.read_bytes()[:9000])
        assert "stops part way through" in str(raised.value)


class TestUnknownCurrencies:
    def test_a_currency_this_project_does_not_model_is_skipped(self) -> None:
        """Six ledgers in this recording that CipherChain has no adapter for.
        None of their addresses may appear on any claim: a label on a chain
        nothing can trace looks like coverage and provides none."""
        published = {claim.address for claim in claims()}
        assert not published.intersection(UNMODELLED_ADDRESSES)

    def test_an_unmodelled_ledger_is_never_resolved_by_elimination(self) -> None:
        """The dangerous shape. This entry lists the SAME 0x address under both
        `ETH` and `ETC`, and an 0x string says nothing about which EVM ledger it
        is on. The ETH row is why the address is claimed at all; the ETC row
        must contribute nothing rather than a second, Ethereum-Classic-shaped
        guess filed as `ethereum`."""
        rows = [c for c in claims() if c.address == "0xd882cfc20f52f2599d84b8e8d58c7fb62cfe344b"]
        assert [claim.chain for claim in rows] == ["ethereum"]

    def test_a_ticker_nobody_has_seen_before_is_skipped_not_guessed(self) -> None:
        """OFAC adds tickers without warning — `ARB` and `BSC` are recent. A
        new one on a familiar-looking address must drop out and be counted, not
        be filed on whichever chain the encoding resembles."""
        renamed = SDN.read_bytes().replace(
            b"<idType>Digital Currency Address - ETH</idType>\n"
            b"        <idNumber>0x19F8f2B0915Daa12a3f5C9CF01dF9E24D53794F7</idNumber>",
            b"<idType>Digital Currency Address - NEWCHAIN</idType>\n"
            b"        <idNumber>0x19F8f2B0915Daa12a3f5C9CF01dF9E24D53794F7</idNumber>",
        )
        assert renamed != SDN.read_bytes()  # the replacement actually matched

        published = {claim.address for claim in claims(renamed)}
        assert "0x19F8f2B0915Daa12a3f5C9CF01dF9E24D53794F7" not in published
        assert len(published) == len({claim.address for claim in claims()}) - 1

    def test_a_row_whose_party_has_no_name_is_dropped_rather_than_placeheld(self) -> None:
        """A claim's whole value is the name on it. "OFAC SDN listed address"
        would be a placeholder sitting where a reader expects an entity, and
        the frozen taxonomy is explicit that only a claim that NAMES may name."""
        nameless = SDN.read_bytes().replace(b"<lastName>OKO DESIGN BUREAU</lastName>\n    ", b"")
        assert nameless != SDN.read_bytes()

        published = {claim.address for claim in claims(nameless)}
        assert "13fhnkmpBBWXUQucJd6efWvXdEj78DKavk" not in published
        assert "0x19F8f2B0915Daa12a3f5C9CF01dF9E24D53794F7" not in published

    def test_a_complete_document_with_nothing_in_it_is_a_schema_change(self) -> None:
        """OFAC has designated hundreds of addresses on chains this project
        traces, so a whole document yielding none is the schema having moved.
        Refusing keeps yesterday's rows standing; a silent empty would let them
        age out while the cycle reported success."""
        emptied = SDN.read_bytes().replace(b"Digital Currency Address", b"Passport")
        with pytest.raises(SourceRejected, match="schema has changed"):
            claims(emptied)


class TestChainMapping:
    def test_only_ledgers_with_an_adapter_are_mapped(self) -> None:
        """A ticker mapped to a chain the engine cannot resolve produces labels
        that can never match anything."""
        assert set(SDN_LEDGER_CHAINS.values()) <= {"bitcoin", "ethereum", "tron", "solana"}

    def test_the_bitcoin_shapes_agree_with_the_chain_adapter(self) -> None:
        """`parsers._BITCOIN_ADDRESS` is a copy of the adapter's patterns — a
        parser is a pure function of bytes and cannot build a provider pool to
        ask. This is the pin that makes drift show up here rather than as a
        USDT-on-Omni address filed on a chain the adapter does not recognise."""
        from cipherchain.chains.bitcoin.adapter import _BECH32, _P2PKH_P2SH
        from cipherchain.harvest.parsers import _BITCOIN_ADDRESS

        for claim in claims():
            recognised = bool(_P2PKH_P2SH.match(claim.address) or _BECH32.match(claim.address))
            assert bool(_BITCOIN_ADDRESS.match(claim.address)) is recognised
            assert (claim.chain == "bitcoin") is recognised


class TestATickerThatContradictsItsAddress:
    """The ticker is a typed field on a form; the encoding is objective.

    OFAC publishes one row (2026-08-18) whose two halves cannot both be true —
    Bitcoin's ticker on a Tron account. Filed where the ticker says, that
    address sits on `bitcoin`, where no Tron trace can ever match it: "not
    partial coverage, a label that can never match and a sanctioned address
    that is effectively missing", in the parser's own words. So the encoding
    decides where it is filed, and the publisher's disagreement is recorded on
    the claim rather than swallowed — a reader who finds a Tron label sourced
    from an XBT row has to be able to see that OFAC said XBT.
    """

    def test_the_address_encoding_decides_the_chain_and_not_the_ticker(self) -> None:
        chains = {claim.address: claim.chain for claim in claims(with_misfiled_row())}
        assert chains[MISFILED_ADDRESS] == "tron"

    def test_the_publishers_disagreement_is_visible_on_the_claim(self) -> None:
        """Both halves have to be readable off the claim itself: what OFAC said
        (XBT) and what it was filed as (tron). The store keeps the entity
        string, so this is what an investigator reading a sanctions hit sees."""
        entities = {claim.address: claim.entity for claim in claims(with_misfiled_row())}
        recorded = entities[MISFILED_ADDRESS]
        assert "OKO DESIGN BUREAU" in recorded
        assert "XBT" in recorded
        assert "tron" in recorded

    def test_recording_it_does_not_stop_the_claim_corroborating(self) -> None:
        """The note goes in a parenthetical because that is this project's
        annotation syntax (`policy.entity_stem` strips it). Written any other
        way it would change the party's name, and a claim that stems
        differently silently stops corroborating — and being corroborated by —
        every other claim about the same party."""
        by_address = {claim.address: claim.entity for claim in claims(with_misfiled_row())}
        assert entity_stem(by_address[MISFILED_ADDRESS]) == entity_stem(by_address[BITCOIN_SHAPED])

    def test_the_mismatch_is_counted_and_logged_rather_than_absorbed(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        """One row in 977 today. The number is the point: a publisher that
        starts contradicting itself on hundreds of rows changes what this
        parser is doing wholesale, and nothing else in the cycle would say so —
        the claims all land, on chains the tickers never named."""
        with caplog.at_level(logging.WARNING, logger=PARSER_LOG):
            claims(with_misfiled_row())
        logged = " ".join(record.getMessage() for record in caplog.records)
        assert "1 row" in logged
        assert "XBT" in logged
        assert "tron" in logged

    def test_a_row_whose_ticker_and_encoding_agree_is_left_exactly_alone(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        """No false positives, over all 39 digital-currency rows of the
        recording: the XBT rows are Bitcoin addresses, the Omni-USDT row is a
        Bitcoin address a token ticker never claimed otherwise, and not one
        claim carries the annotation or the warning."""
        with caplog.at_level(logging.WARNING, logger=PARSER_LOG):
            parsed = claims()
        assert caplog.records == []
        assert not [claim for claim in parsed if "encoding" in claim.entity]
        chains = {claim.address: claim.chain for claim in parsed}
        assert chains[BITCOIN_SHAPED] == "bitcoin"  # XBT, on a Bitcoin address
        assert chains["3E6ZCKRrsdPc35chA9Eftp1h3DLW18NFNV"] == "bitcoin"  # Omni USDT
        assert chains["TFdTr9C3BqQrzKBXqSxJfAZFTh8UwBAfSg"] == "tron"

    def test_an_encoding_that_merely_fails_to_rule_the_ticker_out_is_not_a_contradiction(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        """Solana is the limit of the rule, and it is the same limit that makes
        a USDT-on-Solana row unresolvable: a Solana account is base58 with no
        prefix, so a Bitcoin-shaped string is also a possible Solana account.
        The encoding has not contradicted the ticker there, it has only failed
        to rule it out — and moving the row on that would file a real SOL
        designation on a chain where no Solana trace can match.

        Constructed, unlike the XBT row: OFAC publishes no such row, and the
        point is precisely that this parser would not act on one.
        """
        constructed = SDN.read_bytes().replace(
            b"<idNumber>6wjqWWra8ombzaw6VHrG5xpQ972jCYF6bbHiFCbWmr4U</idNumber>",
            b"<idNumber>" + BITCOIN_SHAPED.encode() + b"</idNumber>",
        )
        assert constructed != SDN.read_bytes()

        with caplog.at_level(logging.WARNING, logger=PARSER_LOG):
            parsed = claims(constructed)
        assert caplog.records == []
        assert [claim.chain for claim in parsed if claim.address == BITCOIN_SHAPED] == [
            "bitcoin",
            "solana",
        ]


class TestFetching:
    async def test_the_signed_handoff_to_ofacs_own_bucket_is_followed(self) -> None:
        """The endpoint answers 302 onto a one-hour signed url in OFAC's
        GovCloud bucket. That is a handoff, not a relocation, so it is followed
        — and the citation stays the published url, because the url the
        redirect leads to has expired long before any reader opens the
        report."""
        source = http_source(serving(SDN.read_bytes()))
        loaded = await source.load()
        assert loaded.origin == OFAC_SDN_URL
        assert len(source.parse(loaded, retrieved_at=NOW)) == 30

    async def test_a_redirect_anywhere_else_stops_the_source(self) -> None:
        """The guard the exchange sources needed: a document that has MOVED
        must be read by a person before anything is harvested off it. Only the
        one bucket host is pre-declared."""
        source = http_source(
            routed(
                {
                    OFAC_SDN_URL: httpx.Response(
                        302, headers={"Location": "https://elsewhere.test/SDN.XML"}
                    )
                }
            )
        )
        with pytest.raises(SourceUnavailable, match="has moved"):
            await source.load()

    async def test_a_handoff_offered_in_cleartext_is_refused_even_by_the_right_host(
        self,
    ) -> None:
        """The host is not the whole of the check. Over plain http the entire
        sanctions list arrives in the clear and lands as `active`
        first-party labels that need no corroboration to name anybody — so an
        on-path attacker who rewrote it could un-sanction any address in it,
        and the cycle would report an ordinary morning's harvest. The bucket is
        pre-declared for a signed HTTPS handoff; it was never declared for
        this."""
        cleartext = SIGNED_URL.replace("https://", "http://", 1)
        source = http_source(
            routed(
                {
                    OFAC_SDN_URL: httpx.Response(302, headers={"Location": cleartext}),
                    # Served, and served the real document — so what this pins
                    # is the refusal, not a url that happens to be dead.
                    cleartext: httpx.Response(200, content=SDN.read_bytes()),
                }
            )
        )
        with pytest.raises(SourceUnavailable, match="cleartext"):
            await source.load()

    async def test_a_host_with_no_route_falls_through_to_the_operators_drop(
        self, tmp_path: Path
    ) -> None:
        """Losing the route slows the sanctions list to a person's pace instead
        of removing it. Same source identity either way — two identities would
        let a fetched copy and a saved copy of one document corroborate each
        other."""
        shutil.copyfile(SDN, tmp_path / "ofac-sdn__2026-08-18.xml")
        source = ofac_sdn_source(tmp_path, unreachable())
        loaded = await source.load()
        assert loaded.origin == OFAC_SDN_URL  # the citation, not the operator's disk
        assert len(source.parse(loaded, retrieved_at=NOW)) == 30

    async def test_a_drop_that_is_not_the_sdn_document_is_not_read(self, tmp_path: Path) -> None:
        """The exchange sources accept a labelpack or a CSV as a drop. Here that
        would let a stray file land as sanctions claims — the most consequential
        category in the store — under OFAC's name."""
        assert set(OFAC_PARSERS) == {"xml"}
        shutil.copyfile(FIXTURES / "binance_labelpack.json", tmp_path / "ofac-sdn__2026-08-18.json")
        source = ofac_sdn_source(tmp_path, unreachable())
        with pytest.raises(SourceUnavailable, match="no drop in"):
            await source.load()


class TestWiring:
    def test_the_sdn_list_runs_in_the_daily_cycle(self, tmp_path: Path) -> None:
        """The whole point of the change: a newly designated address reaches the
        store without anybody doing anything."""
        sources = daily_sources(tmp_path, httpx.AsyncClient())
        assert "ofac-sdn" in {source.spec.name for source in sources}

    def test_the_timeout_fits_the_measured_document_at_the_measured_rate(self) -> None:
        """This is the guard against tidying. 28,812,494 bytes at the ~77 KB/s
        this host gets is about 366 seconds, and the last person to size this
        used 200 and concluded the endpoint was unreachable — which is why
        CipherChain read sanctions from a hand-refreshed snapshot instead of a
        feed. The ceiling must leave room for a slow morning, not merely for a
        perfect one."""
        at_measured_rate = 28_812_494 / (77 * 1024)
        assert at_measured_rate * 2 <= OFAC_SDN_TIMEOUT_SECONDS

    def test_the_staleness_window_suits_a_list_republished_on_every_action(self) -> None:
        """OFAC restates the file whenever it designates anybody — the fetched
        document was published the same day it was measured. A window as loose
        as the monthly default would let a genuinely frozen feed look healthy
        for over a month."""
        assert OFAC_SDN.stale_after_days == 14

    def test_only_ofacs_own_bucket_may_receive_the_handoff(self) -> None:
        hosts = set(OFAC_STORAGE_HOSTS)
        assert hosts == {"wc2h-sls-prod-public-published.s3.us-gov-west-1.amazonaws.com"}

    def test_the_urls_that_do_not_serve_the_document_do_not_come_back(self) -> None:
        """Three were tried and recorded in `sanctions.py`. The
        `ofac.treasury.gov/media` one is the trap: it answers a 404 PAGE, so a
        source pointed at it would fetch bytes successfully every morning."""
        assert OFAC_SDN.document_url == OFAC_SDN_URL
        assert "PublicationPreview" not in OFAC_SDN_URL
        assert "treasury.gov/ofac/downloads" not in OFAC_SDN_URL


class TestIdempotence:
    """The cycle over real storage. Needs the Postgres fixture; skips without it."""

    async def cycle(
        self, sessions: async_sessionmaker[AsyncSession], drop_dir: Path
    ) -> HarvestWorker:
        return HarvestWorker(sessions, [ofac_sdn_source(drop_dir, unreachable())])

    async def events(self, sessions: async_sessionmaker[AsyncSession]) -> list[Any]:
        async with sessions() as session:
            return await LabelRepository(session).events_after(0, limit=1000)

    async def test_a_second_cycle_over_an_unchanged_list_writes_no_event(
        self, sessions: async_sessionmaker[AsyncSession], tmp_path: Path
    ) -> None:
        """OFAC republishes the same document until it designates somebody. An
        audit trail that logged all 900-odd addresses every morning would drown
        the designations it exists to show."""
        shutil.copyfile(SDN, tmp_path / "ofac-sdn__2026-08-18.xml")
        first = await (await self.cycle(sessions, tmp_path)).run()
        assert next(o for o in first.sources if o.source == "ofac-sdn").added == 30
        assert len(await self.events(sessions)) == 30

        second = await (await self.cycle(sessions, tmp_path)).run()

        outcome = next(o for o in second.sources if o.source == "ofac-sdn")
        assert (outcome.unchanged, outcome.added, outcome.updated) == (30, 0, 0)
        assert len(await self.events(sessions)) == 30

    async def test_the_designated_party_is_what_lands_in_the_store(
        self, sessions: async_sessionmaker[AsyncSession], tmp_path: Path
    ) -> None:
        """Active on arrival, because OFAC publishing its own list is a trusted
        tier — and naming the DESIGNATED PARTY, not the publisher. A store full
        of rows saying "OFAC" would name nobody."""
        shutil.copyfile(SDN, tmp_path / "ofac-sdn__2026-08-18.xml")
        await (await self.cycle(sessions, tmp_path)).run()

        async with sessions() as session:
            rows = await LabelRepository(session).active_labels()
        sanctions = [row for row in rows if row.source == "ofac-sdn"]
        assert len(sanctions) == 30
        assert {row.category for row in sanctions} == {"sanctioned"}
        assert "GARANTEX EUROPE OU (CYBER4, RUSSIA-EO14024)" in {row.entity for row in sanctions}

    async def test_a_truncated_list_contributes_nothing_and_keeps_yesterdays_rows(
        self, sessions: async_sessionmaker[AsyncSession], tmp_path: Path
    ) -> None:
        """The failure this source is designed around, at cycle level. Yesterday
        the whole list landed; today the download is cut off. The right outcome
        is a named failure and 30 rows still standing — NOT 4 rows and a report
        that reads like a quiet day."""
        drop = tmp_path / "ofac-sdn__2026-08-18.xml"
        shutil.copyfile(SDN, drop)
        await (await self.cycle(sessions, tmp_path)).run()

        drop.write_bytes(SDN.read_bytes()[:9000])
        report = await (await self.cycle(sessions, tmp_path)).run()

        outcome = next(o for o in report.sources if o.source == "ofac-sdn")
        assert outcome.claims == 0
        assert "stops part way through" in (outcome.error or "")
        async with sessions() as session:
            rows = await LabelRepository(session).active_labels()
        assert len([row for row in rows if row.source == "ofac-sdn"]) == 30
