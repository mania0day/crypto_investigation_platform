"""One parser per published format. Pure functions of bytes, deliberately.

Every one of these is total about what it refuses. A harvest source feeds the
attributor, and the attributor is what puts an operator's name on an address in
a document that goes to a regulating body — so a row this layer cannot read
exactly is dropped and counted, never repaired by assumption.

Why this does not call ``load_labelpack``
-----------------------------------------
It reads the same file format, but it needs one field ``LabelPack`` drops:
``method``. Method is what decides whether a claim may name anybody at all
(``policy.arrival_status``), and a loader that discards it cannot serve a path
where the answer matters. Reusing it and then re-opening the file to recover
``method`` would give two readings of one document that could disagree.
"""

from __future__ import annotations

import csv
import io
import json
import logging
import re
from collections import Counter
from collections.abc import Mapping
from datetime import UTC, datetime
from typing import Any
from xml.etree import ElementTree
from xml.parsers.expat import errors as expat_errors

from cipherchain.analysis.attribution.labels import normalize_address
from cipherchain.harvest.sources import (
    DocumentParser,
    HarvestDocument,
    SourceRejected,
    SourceSpec,
    SourceUnavailable,
)
from cipherchain.intel.policy import IntelClaim

logger = logging.getLogger(__name__)

# The network column of a published proof-of-reserves file, mapped onto CipherChain
# chain ids. `scripts/import_por_labelpack.py` keeps its own copy on purpose:
# that script can only admit chains whose SIGNATURES it is able to check, which
# is a strictly smaller set than the chains an address can be normalized on.
POR_NETWORKS: Mapping[str, str] = {
    "ETH": "ethereum",
    "ETHEREUM": "ethereum",
    "POLYGON": "polygon",
    "MATIC": "polygon",
    "TRON": "tron",
    "TRX": "tron",
    "BTC": "bitcoin",
    "BITCOIN": "bitcoin",
    "SOL": "solana",
    "SOLANA": "solana",
}


def parse_labelpack(
    document: HarvestDocument, *, spec: SourceSpec, retrieved_at: datetime
) -> list[IntelClaim]:
    """The repo's own labelpack format — the shape ``labels/`` already uses.

    The pack's declared ``source`` must match the source that is harvesting it.
    Without that check a drop named ``binance-...`` could carry a pack claiming
    to be some other source, and the store's claim identity is
    ``(chain, address, source)`` — so it would land as a DIFFERENT source's
    claim and could then corroborate the real one. A source must never be able
    to manufacture its own corroborator.
    """
    try:
        raw: Any = json.loads(document.raw)
    except ValueError as exc:
        raise SourceRejected(f"{spec.name}: not valid JSON ({exc})") from exc
    if not isinstance(raw, dict):
        raise SourceRejected(f"{spec.name}: labelpack must be a JSON object")
    declared_source = str(raw.get("source") or "")
    if declared_source != spec.name:
        raise SourceRejected(
            f"{spec.name}: labelpack declares source {declared_source!r} — a source may "
            "not ingest another source's claims under its own name"
        )
    method = str(raw.get("method") or spec.method)
    if method != spec.method:
        raise SourceRejected(
            f"{spec.name}: labelpack declares method {method!r}, source is {spec.method!r}"
        )
    source_date = _date(raw.get("source_date"), spec)
    try:
        default_confidence = float(raw.get("default_confidence", spec.confidence))
    except (TypeError, ValueError) as exc:
        raise SourceRejected(
            f"{spec.name}: default_confidence {raw.get('default_confidence')!r} is not a number"
        ) from exc
    entries = raw.get("labels")
    if not isinstance(entries, list):
        raise SourceRejected(f"{spec.name}: labelpack has no 'labels' list")
    claims: list[IntelClaim] = []
    for index, entry in enumerate(entries):
        try:
            claims.append(
                IntelClaim(
                    chain=str(entry["chain"]),
                    address=str(entry["address"]),
                    entity=str(entry.get("entity") or spec.entity),
                    category=str(entry.get("category") or spec.category),
                    role=str(entry.get("role") or spec.role),
                    confidence=float(entry.get("confidence", default_confidence)),
                    method=spec.method,
                    source=spec.name,
                    retrieved_at=retrieved_at,
                    source_date=source_date,
                    evidence_url=document.origin,
                )
            )
        except (KeyError, TypeError, ValueError) as exc:
            raise SourceRejected(f"{spec.name}: labelpack entry {index}: {exc}") from exc
    return claims


def parse_proof_of_reserves_csv(
    document: HarvestDocument, *, spec: SourceSpec, retrieved_at: datetime
) -> list[IntelClaim]:
    """An exchange's published proof-of-reserves address list.

    These files open with a per-coin summary table, so the first line is not
    the header; the real header is the row that names both an address and the
    message that was signed over it. Same sniffing as
    ``scripts/import_por_labelpack.py``, which learned it from a live file.

    **This parser does not verify signatures**, and therefore refuses a source
    whose declared method is ``signature``. The signature tier means someone
    checked that the key recovers to the address; recording it here would
    upgrade an unverified file to the strongest provenance CipherChain has, on the
    strength of a filename. Verification lives in the script that owns the
    crypto dependencies — a file run through it becomes a labelpack, and comes
    back through :func:`parse_labelpack` wearing the method it earned.

    Note also what a proof-of-reserves address IS: a wallet holding reserves.
    It answers "these funds reached <entity>" better than it answers "this is
    the wallet user deposits are swept into".
    """
    if spec.method == "signature":
        raise SourceRejected(
            f"{spec.name}: this parser does not check signatures, so it may not record the "
            "'signature' method — run scripts/import_por_labelpack.py and drop the labelpack"
        )
    text = document.raw.decode("utf-8-sig", errors="replace")
    lines = text.splitlines()
    start = next(
        (
            index
            for index, line in enumerate(lines)
            if "address" in line.lower() and "message" in line.lower()
        ),
        None,
    )
    if start is None:
        raise SourceRejected(
            f"{spec.name}: no address/message header row — is this a proof-of-reserves file?"
        )
    source_date = _date(None, spec, fallback=document.declared_date)
    claims: list[IntelClaim] = []
    dropped: dict[str, int] = {}
    for row in csv.DictReader(io.StringIO("\n".join(lines[start:]))):
        clean = {
            key.strip().lower(): value.strip()
            for key, value in row.items()
            if isinstance(key, str) and isinstance(value, str)
        }
        address = clean.get("address", "")
        network = clean.get("network", "").upper()
        if not address:
            dropped["not an address row"] = dropped.get("not an address row", 0) + 1
            continue
        chain = POR_NETWORKS.get(network)
        if chain is None:
            # Named, but on a ledger CipherChain has no adapter for. Counted and
            # dropped: a label on a chain nothing can trace is a row that looks
            # like coverage and provides none.
            key = f"unsupported network {network or 'unknown'}"
            dropped[key] = dropped.get(key, 0) + 1
            continue
        claims.append(
            IntelClaim(
                chain=chain,
                address=address,
                entity=spec.entity,
                category=spec.category,
                role=spec.role,
                confidence=spec.confidence,
                method=spec.method,
                source=spec.name,
                retrieved_at=retrieved_at,
                source_date=source_date,
                evidence_url=document.origin,
            )
        )
    if dropped:
        logger.info(
            "%s: %d row(s) dropped (%s)",
            spec.name,
            sum(dropped.values()),
            ", ".join(f"{reason} x{count}" for reason, count in sorted(dropped.items())),
        )
    if not claims:
        # A published disclosure that yields nothing is a format change, not an
        # exchange that stopped holding funds. Refusing keeps the previous
        # harvest's rows standing instead of quietly ageing into staleness.
        raise SourceRejected(f"{spec.name}: no usable rows in the proof-of-reserves file")
    return claims


# Coinbase's reserves page ships its server-rendered state as one JSON island.
# Keyed on the element id rather than on position, because the page carries
# several <script type="application/json"> blocks and only this one is the
# Relay store.
_SERVER_APP_STATE = re.compile(
    r'<script id="server-app-state" type="application/json">(?P<json>.*?)</script>',
    re.DOTALL,
)


def parse_coinbase_reserves(
    document: HarvestDocument, *, spec: SourceSpec, retrieved_at: datetime
) -> list[IntelClaim]:
    """Coinbase's published reserve addresses, read out of the page's own state.

    Measured 2026-08-16: ``https://www.coinbase.com/cbbtc/proof-of-reserves``
    answers HTTP 200 to a plain identifying User Agent, ``robots.txt`` permits
    the path, and the served HTML already contains the answer — 52 Bitcoin
    addresses and a ``lastUpdatedAt`` of ``2026-08-16T16:32:11Z``. This is the
    one exchange disclosure in this package that a host with a route to it can
    read for itself. The url this source was shipped with
    (``/legal/transparency``) answers 404 and had been doing so, silently, for
    as long as the harvester had existed.

    Why the Relay store rather than the rendered table
    --------------------------------------------------
    The page is a React app; the table is built in the browser. What the server
    sends is ``<script id="server-app-state">``, holding the query result
    verbatim: ``ReserveAddress`` records, each pointing at a ``ReserveBalance``
    that names the network, and one ``ProofOfReserves`` record carrying the
    publication timestamp. Reading THAT is reading Coinbase's own data
    structure, and it is the reason this parser can key on field names instead
    of on markup that a redesign rewrites.

    Emptiness is proved, never inferred
    -----------------------------------
    Every structural marker this parser depends on is asserted, and a missing
    one raises. Zero rows would mean "Coinbase holds no reserves", which is the
    class of answer (``explorer_fetch``'s false empty, the proof-of-reserves
    parser's format change) that is worse than an error — an error leaves
    yesterday's rows standing and puts a line in the cron mail, and a false
    empty ages coverage out in silence.

    What these addresses ARE
    ------------------------
    Wallets Coinbase says hold the reserves backing cbBTC. That answers "these
    funds reached Coinbase" well. It is not a customer deposit address, and
    nothing here upgrades it to one; deposit-side coverage comes from
    clustering (REACHING_THE_VASP.md §6.1). Note also that only the Bitcoin
    side is usable: Coinbase publishes the same page for cbXRP, cbDOGE, cbLTC
    and cbADA, and CipherChain has no adapter for any of those ledgers, so those
    rows would be dropped by the same rule that drops the TON row from a
    proof-of-reserves CSV.
    """
    if spec.method == "signature":
        # Same refusal as the proof-of-reserves parser, for the same reason:
        # the page publishes no signatures, so nothing here checked one.
        raise SourceRejected(
            f"{spec.name}: this parser does not check signatures, so it may not record the "
            "'signature' method — the reserves page publishes addresses, not proofs"
        )
    text = document.raw.decode("utf-8", errors="replace")
    island = _SERVER_APP_STATE.search(text)
    if island is None:
        raise SourceRejected(
            f'{spec.name}: no <script id="server-app-state"> block — the reserves page no '
            "longer serves its state, or what came back is not that page"
        )
    records = _relay_records(island.group("json"), spec)
    # Truncated to the UTC day, and that is not cosmetic. The page's
    # `lastUpdatedAt` moves every time Coinbase recomputes balances — it moved
    # by fifteen minutes between two fetches while this was being written —
    # while the ADDRESS SET it publishes changes rarely. `source_date` is part
    # of a claim's identity in the store, so a second-precision timestamp made
    # every cycle report 52 rows `updated` over a document saying the same
    # thing, and made two runs on the same day non-idempotent, which the cycle
    # is documented to be. A day is also the granularity every other source in
    # this package works in: a drop file is dated `YYYY-MM-DD`, and a labelpack
    # declares a date. The residual is that a same-day republication is not
    # distinguished from the morning's; nothing downstream weighs a claim by
    # the hour, and the staleness alarm's tightest window is three days.
    source_date = _date(
        _published_at(records, spec), spec, fallback=document.declared_date
    ).replace(hour=0, minute=0, second=0, microsecond=0)

    claims: list[IntelClaim] = []
    dropped: dict[str, int] = {}
    for record in records.values():
        if record.get("__typename") != "ReserveAddress":
            continue
        address = str(record.get("address") or "")
        if not address:
            dropped["reserve address with no address"] = (
                dropped.get("reserve address with no address", 0) + 1
            )
            continue
        # The network lives on a separate record the address points at. Chase
        # the pointer rather than assuming the two are adjacent: the store is a
        # map, and map order is not a contract.
        balance = record.get("balance")
        reference = balance.get("__ref") if isinstance(balance, dict) else None
        linked = records.get(str(reference)) if reference is not None else None
        network = str((linked or {}).get("network") or "").upper()
        chain = POR_NETWORKS.get(network)
        if chain is None:
            key = f"unsupported network {network.lower() or 'unknown'}"
            dropped[key] = dropped.get(key, 0) + 1
            continue
        claims.append(
            IntelClaim(
                chain=chain,
                address=address,
                entity=spec.entity,
                category=spec.category,
                role=spec.role,
                confidence=spec.confidence,
                method=spec.method,
                source=spec.name,
                retrieved_at=retrieved_at,
                source_date=source_date,
                evidence_url=document.origin,
            )
        )
    if dropped:
        logger.info(
            "%s: %d reserve row(s) dropped (%s)",
            spec.name,
            sum(dropped.values()),
            ", ".join(f"{reason} x{count}" for reason, count in sorted(dropped.items())),
        )
    if not claims:
        raise SourceRejected(
            f"{spec.name}: the reserves page yielded no address on a chain CipherChain can trace — "
            "a layout change or a page for another asset, not an exchange holding nothing"
        )
    return claims


def _relay_records(island: str, spec: SourceSpec) -> Mapping[str, Mapping[str, Any]]:
    """The record map, two JSON layers down, with every layer checked.

    ``relayStoreData`` is a JSON *string* inside the outer JSON object — the
    page double-encodes it — so this is two decodes, and both of them are a
    place the page can change shape without telling anybody.
    """
    try:
        outer: Any = json.loads(island)
    except ValueError as exc:
        raise SourceRejected(f"{spec.name}: server-app-state is not valid JSON ({exc})") from exc
    if not isinstance(outer, dict) or "relayStoreData" not in outer:
        raise SourceRejected(f"{spec.name}: server-app-state carries no 'relayStoreData'")
    try:
        store: Any = json.loads(outer["relayStoreData"])
    except (TypeError, ValueError) as exc:
        raise SourceRejected(f"{spec.name}: relayStoreData is not valid JSON ({exc})") from exc
    records = store.get("recordMap") if isinstance(store, dict) else None
    if not isinstance(records, dict) or not records:
        raise SourceRejected(f"{spec.name}: relayStoreData carries no 'recordMap'")
    return {str(key): value for key, value in records.items() if isinstance(value, dict)}


def _published_at(records: Mapping[str, Mapping[str, Any]], spec: SourceSpec) -> str:
    """The disclosure's own date — the OLDEST one the page carries.

    A page holding two disclosures is only as fresh as its stale half, and this
    date is what the staleness alarm reads. Taking the newest would let one
    hourly-updated record vouch for a table nobody has touched since spring.
    """
    stamps = sorted(
        str(record["lastUpdatedAt"])
        for record in records.values()
        if record.get("__typename") == "ProofOfReserves" and record.get("lastUpdatedAt")
    )
    if not stamps:
        raise SourceRejected(
            f"{spec.name}: no ProofOfReserves record with a lastUpdatedAt — the page states no "
            "publication date, and a claim a reader cannot date is a claim they cannot weigh"
        )
    return stamps[0]


# OFAC writes every crypto identifier into an `id` row as
# `Digital Currency Address - <TICKER>`. Anchored at both ends because the same
# `idList` carries prose ids ("Executive Order 13846 information") and an
# unanchored match would read one of those as a currency.
_DIGITAL_CURRENCY_ID = re.compile(
    r"^Digital Currency Address\s*-\s*(?P<ticker>[A-Za-z0-9]+)$", re.IGNORECASE
)

# Ticker -> the ledger it names. Only tickers that name a ledger CipherChain has
# an adapter for are here, and nothing else is guessed: an address recorded on
# the wrong chain is not partial coverage, it is a label that can never match
# and a sanctioned address that is effectively missing. Polygon is modelled and
# absent on purpose — no SDN ticker names it, and inventing one would be the
# same guess in the other direction.
#
# Measured against the document published 2026-08-18 (977 digital-currency
# rows): XBT 529, TRX 198, ETH 100, USDT 93, SOL 4, USDC 2 are covered here;
# LTC 14, XMR 11, BCH 7, DASH 5, ZEC 4, DOGE 2, and one each of XRP, XVG, ETC,
# BTG, BSV, BSC, BNB and ARB are counted as skipped, so a ledger worth adding
# shows up in the cycle log rather than in nobody's notes. BSC, BNB and ARB are
# the ones to look at first — they are EVM addresses this refuses to file under
# `ethereum`, because the same string on a different EVM chain is a different
# balance and a trace would follow the wrong ledger.
SDN_LEDGER_CHAINS: Mapping[str, str] = {
    "XBT": "bitcoin",
    # Not seen in the live document — OFAC writes Bitcoin as XBT — and kept
    # because a publisher switching to the commoner spelling must not silently
    # drop 500-odd addresses on the day it does.
    "BTC": "bitcoin",
    "ETH": "ethereum",
    "TRX": "tron",
    "SOL": "solana",
}

# Tickers that name an ASSET rather than a ledger. OFAC's idType says which
# token, never which chain, and the same token lives on several: the 2026-08-18
# document carries USDT rows on Ethereum, on Tron, and — seven of them — as
# Omni-layer USDT on plain Bitcoin addresses. These are resolved from the
# address's own encoding below, which is the one thing in the row that states
# the ledger.
SDN_TOKEN_TICKERS = frozenset({"USDT", "USDC"})

# The expat error codes that mean THE INPUT RAN OUT, as against a body that
# arrived whole and is broken. ElementTree reports both as one exception class,
# and the two need opposite things from an operator — wait for tomorrow's fetch,
# against go and read what the publisher is serving. `scripts/harvest.sh` prints
# an instruction per message, so telling them apart is what makes that
# instruction true; a malformed document reported as a cut-off one sends
# somebody to re-run a download that will fail the same way every morning.
# Every one of these was produced by cutting a real document: the SDN list cut
# mid-entry and cut just before its closing tag both give "no element found",
# and a cut landing inside a tag, a comment or a UTF-8 sequence gives the other
# three.
_TRUNCATED_XML_CODES = frozenset(
    expat_errors.codes[name]
    for name in (
        expat_errors.XML_ERROR_NO_ELEMENTS,
        expat_errors.XML_ERROR_UNCLOSED_TOKEN,
        expat_errors.XML_ERROR_PARTIAL_CHAR,
        expat_errors.XML_ERROR_UNCLOSED_CDATA_SECTION,
    )
)

_EVM_ADDRESS = re.compile(r"^0x[0-9a-fA-F]{40}$")
# Tron's base58check form: leading T, 34 characters total.
_TRON_ADDRESS = re.compile(r"^T[1-9A-HJ-NP-Za-km-z]{33}$")
# Bitcoin's, mirroring `chains/bitcoin/adapter.py`. Copied rather than imported
# because a parser is a pure function of bytes and must not have to build a
# provider pool to read a file; the shapes are pinned against the adapter's by
# test, so drift shows up there rather than as a wrong-chain label.
_BITCOIN_ADDRESS = re.compile(r"^([13][a-km-zA-HJ-NP-Z1-9]{25,34}|(bc1|BC1)[a-zA-Z0-9]{11,71})$")


def parse_ofac_sdn(
    document: HarvestDocument, *, spec: SourceSpec, retrieved_at: datetime
) -> list[IntelClaim]:
    """OFAC's SDN list, read whole or not at all.

    Why completeness is checked before a single claim is emitted
    ------------------------------------------------------------
    This is the one parser here whose document is too big to arrive in one
    piece. Measured from this host on 2026-08-18: the SDN XML is **28,812,494
    bytes** and streams at roughly 77 KB/s — about six minutes — and the
    publisher's endpoint sends it ``Transfer-Encoding: chunked``, so there is
    **no Content-Length to compare a short file against**. A download cut off
    part way leaves something that still looks like a sanctions list: the probe
    that stopped at 15,388,672 bytes (53% of it) held 9,959 complete
    ``<sdnEntry>`` elements and 564 of the document's 977 digital-currency
    rows. Ingesting that would retire nothing and add nothing visible, and
    every address past the cut would simply not be sanctioned as far as
    CipherChain is concerned — traces through them would report "no named
    endpoint" as though the chain were clean. That is a false negative in a
    forensic tool, produced by a partial success that reads like a small day.

    So the document is parsed to its end before anything is built from it, and
    the root's closing element must actually arrive. An XML document has one
    root, so a truncated body cannot be well-formed — completeness is not
    inferred from a byte count, it is proved by the parse. A body that stopped
    mid-stream, and a missing close, are :class:`SourceUnavailable` rather than
    :class:`SourceRejected`: the file is not wrong, it is not all here, and the
    right response is to keep yesterday's rows and fetch again tomorrow.

    A document that arrived WHOLE and is malformed is the other answer. It is
    the same exception out of ``ElementTree`` and it needs the opposite of the
    operator (:data:`_TRUNCATED_XML_CODES` is what separates them), because
    re-running fetches the same bytes and fails identically: somebody has to
    look at what the publisher is serving.

    Claims are built in a second pass, after that proof. Nothing else enforces
    the ordering — a parser that appended as it went would hold a list of real
    claims at the moment it discovered the document was short, and the next
    person to touch it would find returning them very reasonable.

    What lands on the claim
    -----------------------
    The listed party's own name and the sanctions programs it was listed
    under — ``ROSFINMONITORING (RUSSIA-EO14024, CYBER2)``. The programs go in
    the parenthetical because that is this project's annotation syntax
    (``policy.entity_stem`` strips it), so an investigator reads which
    authority listed the address while corroboration still matches on the party
    being named rather than on the programme list of the day.

    Chains, and what is skipped
    ---------------------------
    :data:`SDN_LEDGER_CHAINS` maps the tickers that name a ledger; token
    tickers (:data:`SDN_TOKEN_TICKERS`) are resolved from the address encoding,
    since ``Digital Currency Address - USDT`` is published for both Ethereum
    and Tron addresses and the ticker alone does not say which. Everything else
    is counted and dropped.

    Where a row's two halves contradict each other, the ENCODING decides and
    the disagreement is recorded on the claim (:func:`_sdn_chain`). It is one
    row in 977 today, and it is also counted and logged, because a publisher
    that started contradicting itself wholesale would otherwise change what
    this parser files without anything anywhere saying so.

    On parsing this with the standard library: measured on this interpreter,
    ``ElementTree`` resolves no external entity — an ``XXE`` reference comes
    back as a parse error, with nothing read off this host — and expat raises
    on its own input-amplification limit rather than expanding a nested-entity
    bomb. Both land as malformed documents, above. That is the floor and not
    the argument: this document is fetched over TLS from the publisher or
    dropped by the operator, and a third party able to substitute it could
    substitute the sanctions list itself, which is the larger problem by far.
    """
    rows: list[tuple[str, str, str]] = []  # chain, address, entity
    dropped: Counter[str] = Counter()
    refiled: Counter[str] = Counter()
    published: str | None = None
    root: ElementTree.Element | None = None
    closed = False
    entries = 0
    try:
        for event, element in ElementTree.iterparse(
            io.BytesIO(document.raw), events=("start", "end")
        ):
            if event == "start":
                if root is None:
                    root = element
                    if _local(element.tag) != "sdnList":
                        raise SourceRejected(
                            f"{spec.name}: root element is <{_local(element.tag)}>, not "
                            "<sdnList> — this is not the SDN document"
                        )
                continue
            name = _local(element.tag)
            if name == "publshInformation":
                # OFAC's own spelling, in the live document and in the
                # published schema. Corrected here it would find nothing.
                published = _child_text(element, "Publish_Date") or published
                element.clear()
            elif name == "sdnEntry":
                entries += 1
                rows.extend(_sdn_entry_rows(element, dropped=dropped, refiled=refiled))
                # The entry's subtree is done with. Without this the whole
                # 28 MB document is held as objects at once; cleared, only the
                # empty shells remain and memory stays flat across 20,000
                # entries.
                element.clear()
            elif element is root:
                closed = True
    except ElementTree.ParseError as exc:
        read = f"{len(document.raw)} bytes, {entries} entries read"
        if exc.code in _TRUNCATED_XML_CODES:
            raise SourceUnavailable(
                f"{spec.name}: the document stops part way through ({exc}) — {read}. "
                "A partial sanctions list is not a short one: every address past the cut "
                "would silently stop being sanctioned"
            ) from exc
        # The bytes stopped where the document meant to stop and it is still
        # not readable, so there is nothing to wait for. Rejected rather than
        # unavailable because those two words are the operator's instruction:
        # unavailable means yesterday's rows stand and tomorrow's fetch may
        # work, and this one will fail identically at 03:15 every morning until
        # somebody opens the document.
        raise SourceRejected(
            f"{spec.name}: the document is not well-formed XML ({exc}) — {read}, and it did "
            "NOT stop mid-stream. Re-running fetches the same bytes and fails identically: "
            "read what the publisher is serving before retrying"
        ) from exc
    if not closed:
        # Unreachable through ElementTree, which raises above on an unclosed
        # root. Kept because the whole source rests on this one guarantee, and
        # a guarantee borrowed from a library's error behaviour should be
        # stated where it is relied on rather than assumed to hold forever.
        raise SourceUnavailable(
            f"{spec.name}: the document never closed its <sdnList> — it is incomplete"
        )
    source_date = _sdn_publish_date(published, spec, fallback=document.declared_date)

    claims: list[IntelClaim] = []
    seen: set[tuple[str, str]] = set()
    for chain, address, entity in rows:
        # Deduplicated on the STORE's identity, not on the published string:
        # `IntelService.ingest` normalizes before it upserts, so two rows
        # differing only in hex case are one row there. Comparing raw strings
        # here would let both through and the second would overwrite the first
        # every cycle — an `added` then a run of `updated` events on a label
        # nothing had actually changed.
        key = (chain, normalize_address(address))
        if key in seen:
            # OFAC lists one address under several tickers: on 2026-08-18, 10
            # addresses carry 12 extra rows between them — an ERC-20 holder
            # published under ETH and USDT and/or USDC (6), an Omni-USDT
            # address also published under XBT (4), and the same XBT row twice
            # (2). The store keys on (chain, address, source), so the
            # duplicates would race to be the row's entity — first one wins,
            # and the count says how often.
            dropped["address listed more than once"] += 1
            continue
        seen.add(key)
        claims.append(
            IntelClaim(
                chain=chain,
                address=address,
                entity=entity,
                category=spec.category,
                role=spec.role,
                confidence=spec.confidence,
                method=spec.method,
                source=spec.name,
                retrieved_at=retrieved_at,
                source_date=source_date,
                evidence_url=document.origin,
            )
        )
    if dropped:
        logger.info(
            "%s: %d id row(s) not used (%s)",
            spec.name,
            sum(dropped.values()),
            ", ".join(f"{reason} x{count}" for reason, count in sorted(dropped.items())),
        )
    if refiled:
        # Louder than the drop line, and deliberately: these rows all became
        # claims, on chains the published ticker did not name, so nothing else
        # in the cycle report would look any different. One row in 977 is the
        # publisher's typo; a hundred would mean the ticker field now means
        # something else, and that is a decision for a person rather than a
        # thing for this parser to keep absorbing.
        logger.warning(
            "%s: %d row(s) filed on the ledger the address encodes, against the ticker (%s)",
            spec.name,
            sum(refiled.values()),
            ", ".join(f"{reason} x{count}" for reason, count in sorted(refiled.items())),
        )
    if not claims:
        # Same rule as every other parser here: emptiness is proved, not
        # inferred. OFAC has designated hundreds of addresses on chains this
        # project traces, so a complete document yielding none is a schema
        # change — and an error keeps yesterday's rows standing where a silent
        # empty would let them age out.
        raise SourceRejected(
            f"{spec.name}: {entries} entries and not one digital-currency address on a chain "
            "CipherChain traces — the SDN schema has changed, not the sanctions"
        )
    logger.info("%s: %d entries, %d sanctioned addresses", spec.name, entries, len(claims))
    return claims


def _sdn_entry_rows(
    entry: ElementTree.Element, *, dropped: Counter[str], refiled: Counter[str]
) -> list[tuple[str, str, str]]:
    """One SDN entry's digital-currency addresses as ``(chain, address, entity)``."""
    ids = [row for node in _children(entry, "idList") for row in _children(node, "id")]
    currencies = [
        (match.group("ticker").upper(), _child_text(row, "idNumber"))
        for row in ids
        if (match := _DIGITAL_CURRENCY_ID.match(_child_text(row, "idType"))) is not None
    ]
    if not currencies:
        return []
    entity = _sdn_entity(entry)
    if not entity:
        # A claim's whole value is the name on it. An address with no listed
        # party names nobody, and inventing "OFAC SDN listed address" here
        # would put a placeholder where a reader expects an entity.
        dropped["listed party has no name"] += len(currencies)
        return []
    rows: list[tuple[str, str, str]] = []
    for ticker, address in currencies:
        if not address:
            dropped["digital currency row with no address"] += 1
            continue
        chain = _sdn_chain(ticker, address)
        if chain is None:
            dropped[
                f"{ticker} address on a ledger CipherChain does not model"
                if ticker in SDN_TOKEN_TICKERS
                else f"unmodelled currency {ticker}"
            ] += 1
            continue
        listed = SDN_LEDGER_CHAINS.get(ticker)
        if listed is not None and listed != chain:
            # The row contradicts itself and the address won, so the claim has
            # to say so. Recorded in a parenthetical because that is this
            # project's annotation syntax — `policy.entity_stem` strips it, so
            # the party is still named the same way and corroboration is
            # untouched — and recorded at all because a reader who finds a
            # `tron` label that came from an `XBT` row would otherwise have no
            # way to see that OFAC said Bitcoin, or that anything was decided
            # here. Swallowing it would leave this parser silently overruling
            # the designating authority.
            refiled[f"{ticker} row on a {chain} address"] += 1
            rows.append(
                (
                    chain,
                    address,
                    f"{entity} (listed by OFAC as {ticker}; "
                    f"filed on {chain} from the address encoding)",
                )
            )
            continue
        rows.append((chain, address, entity))
    return rows


def _sdn_chain(ticker: str, address: str) -> str | None:
    """The ledger an SDN row belongs to, or ``None`` to skip it.

    The ENCODING decides wherever it decides outright, including against a
    ticker that names a ledger. The live document published 2026-08-18 carries
    exactly one row where the two disagree — ``Digital Currency Address - XBT``
    on ``TUCsTq7TofTCJRRoHk6RvhMoS2mJLm5Yzq``, which is ``T`` plus 33 base58
    characters and so is a Tron account and nothing else. Honouring the ticker
    there files a sanctioned address on ``bitcoin``, where, in this module's
    own words, it "is not partial coverage, it is a label that can never match
    and a sanctioned address that is effectively missing". The encoding is a
    property of the address; the ticker is a typed field on a form. So the row
    is filed where it can actually match, and the caller records the
    disagreement on the claim rather than swallowing it.

    Where the encoding does NOT decide, the ticker stands, and Solana is where
    that bites twice. A Solana account is base58 with no prefix, so a
    32-to-44 character base58 string could be either a Solana account or a
    Bitcoin address: a ``USDT`` row on Solana is skipped rather than assigned,
    and a ``SOL`` row on a Bitcoin-shaped string keeps its ticker, because the
    encoding has not contradicted anything there — it has only failed to rule
    Solana out. Both are the same refusal to guess, and a guess either way
    files a sanctioned address where nothing can ever match it.
    """
    encoded = _encoded_chain(address)
    ledger = SDN_LEDGER_CHAINS.get(ticker)
    if ledger is not None:
        if encoded is None or encoded == ledger:
            return ledger
        if ledger == "solana" and encoded == "bitcoin":
            # Not a contradiction, only a shape that fails to rule Solana out —
            # so nothing has been proved and the ticker keeps the row. Acting
            # on it would move a real SOL designation onto bitcoin, where no
            # Solana trace can match it: the same loss this rule exists to
            # prevent, caused by the rule itself.
            return ledger
        return encoded
    if ticker not in SDN_TOKEN_TICKERS:
        return None
    return encoded


def _encoded_chain(address: str) -> str | None:
    """The ledger an address's own encoding states, or ``None`` if it states none."""
    if _EVM_ADDRESS.match(address):
        # Every EVM chain shares an address space, so this says Ethereum where
        # it could equally be Polygon. Ethereum is where USDT and USDC are
        # issued and is the mapping the vendored snapshot
        # (`analysis/sanctions/ofac.py`) already uses, so the two agree rather
        # than contradicting each other on the same address.
        return "ethereum"
    if _TRON_ADDRESS.match(address):
        return "tron"
    # USDT predates every chain it is famous on: it was issued on Bitcoin over
    # Omni first, and OFAC still publishes seven `USDT` rows carrying plain
    # Bitcoin addresses (2026-08-18). Dropping them for not being 0x or T would
    # lose seven sanctioned BITCOIN addresses on a chain this project traces
    # fully — the exact silent gap this source exists to close.
    return "bitcoin" if _BITCOIN_ADDRESS.match(address) else None


def _sdn_entity(entry: ElementTree.Element) -> str:
    """The listed party, annotated with the programs it is listed under."""
    named = " ".join(
        part for part in (_child_text(entry, "firstName"), _child_text(entry, "lastName")) if part
    )
    if not named:
        return ""
    programs = [
        text
        for node in _children(entry, "programList")
        for program in _children(node, "program")
        if (text := (program.text or "").strip())
    ]
    return f"{named} ({', '.join(programs)})" if programs else named


def _sdn_publish_date(
    declared: str | None, spec: SourceSpec, *, fallback: datetime | None
) -> datetime:
    """The list's own publication date — what the staleness alarm reads."""
    if declared:
        for form in ("%m/%d/%Y", "%Y-%m-%d"):
            try:
                return datetime.strptime(declared.strip(), form).replace(tzinfo=UTC)
            except ValueError:
                continue
        raise SourceRejected(
            f"{spec.name}: Publish_Date {declared!r} is not a date this reader knows — OFAC "
            "publishes MM/DD/YYYY"
        )
    if fallback is not None:
        return fallback
    raise SourceRejected(
        f"{spec.name}: no <Publish_Date> in the document — an undated sanctions list cannot "
        "be told from one nobody has republished since spring"
    )


def _local(tag: str) -> str:
    """An element's name without its namespace.

    Read namespace-blind because OFAC's has already moved: the published schema
    says ``tempuri.org``, the served document says the service's own hostname.
    A reader keyed on the URI would refuse a document that is otherwise
    identical.
    """
    return tag.rsplit("}", 1)[-1]


def _child_text(element: ElementTree.Element, name: str) -> str:
    """Text of a DIRECT child, never a descendant: an ``sdnEntry`` carries an
    ``akaList`` full of its own ``lastName`` elements, and a descendant search
    would name the entry after one of its aliases."""
    for child in element:
        if _local(child.tag) == name:
            return (child.text or "").strip()
    return ""


def _children(element: ElementTree.Element, name: str) -> list[ElementTree.Element]:
    return [child for child in element if _local(child.tag) == name]


def _date(declared: object, spec: SourceSpec, *, fallback: datetime | None = None) -> datetime:
    """A claim without a date cannot be weighed, and every label is a claim."""
    if declared:
        try:
            parsed = datetime.fromisoformat(str(declared))
        except ValueError as exc:
            raise SourceRejected(f"{spec.name}: invalid source_date {declared!r}") from exc
        return parsed if parsed.tzinfo else parsed.replace(tzinfo=UTC)
    if fallback is not None:
        return fallback
    raise SourceRejected(
        f"{spec.name}: no source_date — the document declares none and the drop's file "
        "name did not supply one"
    )


# Suffix -> parser. A source may pass its own mapping; this is the default set
# and the reason a new publisher is usually a spec entry rather than new code.
PARSERS_BY_SUFFIX: Mapping[str, DocumentParser] = {
    "json": parse_labelpack,
    "csv": parse_proof_of_reserves_csv,
}
