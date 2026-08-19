"""The fetch tier — public explorer pages, read at crawl speed, last of all.

The instruction this exists for was given three times: *"if api exhausted then
use scraping idc if it takes time"* (REACHING_THE_VASP.md §5, tier 3). It is
the floor under the pool: keyed providers first, then Blockscout's keyless
API, then this. It is not meant to be fast. It is meant to make losing every
quota slow a trace down instead of ending it.

Why this explorer and not the one you first thought of
------------------------------------------------------
``blockscout.py`` already recorded that the obvious targets are dead ends.
Re-measuring on 2026-08-16 for this tier found the same wall plus three more:

- ``etherscan.io/robots.txt`` answers **302** into a challenge, so we never
  learn what it permits — that is a decline, not an invitation to follow it.
- ``blockchair.com`` answers **HTTP 401** with a Qrator JavaScript auth
  bootstrap. Getting past it means executing their bot check.
- ``bitinfocharts.com`` answers **403** to an honest, self-identifying User
  Agent. Making the User Agent look like a browser is the same line.
- ``mempool.space`` / ``blockstream.info`` are JavaScript shells (measured
  2026-08-13): the address never appears in the served HTML.

``3xpl.com`` is the one that is genuinely open: ``robots.txt`` is ``Allow:
/``, the address and transaction pages are **server-rendered**, and no bot
check sits in front of them. So this module encodes 3xpl's page layout
concretely instead of pretending to a generic explorer abstraction it could
not honour. Adding a second explorer means a second parser plus a row in
:data:`DEFAULT_SITES`; it does not mean rewriting the provider.

The second chain, and the second row dialect it needed
------------------------------------------------------
3xpl serves Tron, and Tron carries more VASP labels than anything else CipherChain
knows about. Its module slugs were read off a live page on 2026-08-16 —
``tron-main``, ``tron-internal``, ``tron-trc-10``, ``tron-trc-20``, in the same
seven-pane strip, with the same checked-radio tab marker.

The slugs were never what kept Tron out. This provider emitted
**Etherscan-shaped rows** and ``TronAdapter`` reads a **TronGrid-shaped body** —
``{"data": [...]}`` whose rows carry ``txID``, ``raw_data.contract[]`` and
Base58 addresses, not ``hash``/``from``/``to``. A "tron" row in
:data:`DEFAULT_SITES` alone would have routed Tron history here and handed the
adapter a list where it calls ``.get("data")``. Even the hash regex would have
missed: a Tron transaction id has no ``0x`` prefix, so the EVM pattern matches
nothing on a Tron page — and "nothing" is this tier's worst answer.

So the dialect is a value now (:class:`RowEmitter`), and a site names the one
its chain's adapter reads. It owns the identifier patterns as well as the row
shape, because those fail in the same direction: a pattern that no longer
matches produces an empty pane, and an empty pane is a history that never
happened.

What Tron cost, before this existed: a Tron trace had exactly one provider
(TronGrid, priority 10) — Blockscout is EVM-only and this tier declined the
chain — so a spent or throttled TronGrid ended the trace outright. On the run
that prompted the work, 1589 of 1592 addresses carried no label at all.

Two things Tron is deliberately NOT given here:

- **A cursor.** ``TronAdapter`` pages on TronGrid's opaque ``meta.fingerprint``,
  and nothing on a numbered explorer page can mint one. So the body carries no
  ``meta.links.next`` — ``_feed_fingerprint`` reads that as "this feed is
  finished", which is the honest report of a page this tier cannot continue.
  For the same reason a request arriving WITH a fingerprint is declined rather
  than answered from page one: serving the first page under a continuation
  request would hand back rows the caller already has and then claim the feed
  was exhausted. That decline is an ANSWER — no rows, marked as a read cut
  short — and not an error, because an error is charged to a circuit breaker
  this tier shares across every chain it serves, so declining for Tron would
  take Ethereum's floor out with it (:meth:`_declined_continuation`). When the
  pages-per-call bound stops a read early the answer says so in its manifest
  and in the log, because a stop nobody can see is indistinguishable from an
  address that ended there. ``limit`` is read the
  same way — as the caller's page size, not as a cap this tier trims to. The
  rows are whatever the pages read held, because trimming a page to the number
  asked for would drop rows with no cursor to reach them by afterwards.
- **``tron-internal`` and ``tron-trc-10``.** Both slugs are verified; neither is
  mapped. ``TronAdapter`` asks the pool for ADDRESS_HISTORY and TOKEN_TRANSFERS
  and nothing else, so a row emitted for either would be read by no adapter and
  compared against no vendor body — and a shape nobody consumes is a shape
  nobody notices going wrong. The reason is the CONSUMER, not the vendor:
  TronGrid does carry internal transfers, embedded in the native row as
  ``internal_transactions`` (they are in
  ``tests/chains/fixtures/tron_native.json``), and nothing in this codebase
  reads them from there either.

Boundaries, none of them negotiable
-----------------------------------
Public pages only. No login, no credential, no CAPTCHA handling, no browser
impersonation, no rotating identity, and one honest self-identifying User
Agent that a site operator can read in their logs and block deliberately.
``robots.txt`` is fetched, parsed and cached per host BEFORE any path is
requested, and a disallow means this tier declines — a decline is a correct
outcome, not a puzzle to route around. See :data:`DEFAULT_RATE_PER_SEC` for
the crawl rate and the measurement behind it, and a ``Crawl-delay`` in robots
can only slow us further, never speed us up.

One precision, because "no session" would be an overclaim: the ``AsyncClient``
handed to this provider is shared across the process and, like any HTTP
client, stores and echoes back a ``Set-Cookie`` a site sends — 3xpl sets an
XSRF token and a session id on every page. That is ordinary client behaviour,
it is not something this module arranges, and none of those cookies is what
gets a page served.

The line that is actually held is the one above it: when 3xpl answers 429 with
a "Verify you are not a robot" form, this tier does not fill it in. It raises,
the pool moves on, and the run records the feed as unavailable. Whatever
cookie came with that page is worth nothing, because the clearance is granted
by completing the challenge and completing the challenge is the thing we will
not do.

The rate is a correctness property, not politeness
--------------------------------------------------
Measured against the live site on 2026-08-16: 3xpl publishes its own budget in
``x-ratelimit-limit: 25``, and past it the site does not throttle — it
escalates. First a 429 whose body is a full "Verify you are not a robot"
interstitial, then a 403 carrying ``Retry-After: 3600``: a one-hour block on
the whole host, every chain, every capability. This module used to default to
one request per second, which is 60 a minute against a stated 25, and it
earned that ban within roughly twenty requests.

That is worth being blunt about, because it inverts the tier's whole purpose.
A fetch tier crawling too fast does not read slowly — it stops reading, and it
does so on the very first address of the first trace that reaches it, for a
full hour, across every capability and every chain this file serves. The 429 is
handled correctly on
its own (``_http.perform`` raises ``ProviderRateLimited``, so the pool
penalizes the bucket and retries this provider rather than discarding it); the
403 is not recoverable by any means this module is allowed to use, so the only
defence is not provoking it. Hence a default well under the published budget,
and a page cache so a transaction listed by two modules is read once.

What it will and will not answer
--------------------------------
``address_history``, ``internal_traces`` and ``token_transfers``. All three,
because ``EvmAdapter.address_history`` asks the pool for all three on every
call: a tier that served two of them was not a floor under the pool at all —
the moment the keyed providers and Blockscout were spent, an EVM trace died
on the token fetch instead of slowing down. And the missing one was the one
that mattered. The shipped case study turns on 119,610 USDT arriving from
Binance; USDT is a token, so a scraping tier blind to tokens would have
reported the single most important piece of VASP evidence on that address as
absent and called the chain silent.

Tron answers two of the three. ``TronAdapter`` asks for native history and
TRC-20 and nothing else, so ``tron-internal`` stays unmapped rather than
serving a capability no adapter requests and no vendor body defines.

The token scale, which is why this used to be refused
-----------------------------------------------------
This module previously declined ``token_transfers`` on the grounds that 3xpl
prints a token amount as a decimal string ("323.799356 USDC") and never states
the contract's ``decimals`` — so smallest units could only be recovered by
inferring the scale from however many digits happened to be printed. That
inference is genuinely unsafe: ``assets`` is keyed on ``(chain, kind,
contract)`` with ``decimals`` OUTSIDE the key, so one wrong scale silently
redefines that contract for every row the keyed providers ever wrote. A
missing feed is visible to whoever reads the report; a rescaled amount is not.

The premise was wrong about *where* 3xpl states it. Measured 2026-08-16, the
transaction page does not, but the **asset page does**, under a plain heading:
``/asset/ethereum-erc-20/0xa0b8…`` renders "Decimals: 6". So the scale is read
off the site, one extra page per contract, cached for the process because a
contract's decimals do not change. Nothing is inferred.

Two independent statements by the site then have to agree. 3xpl pads every
printed amount to exactly the asset's decimals — measured across USDT and USDC
(6) and a spread of 18-decimal contracts, including
"54,979,508.000000000000000000" and "0.000090000000000000", trailing zeros and
all — so the count of printed fractional digits is itself a statement of the
scale. A token amount is converted only when that count EQUALS the decimals the
asset page states. If the two disagree, the pages disagree about the contract
and this module raises rather than putting a number off by a power of ten into
evidence.

Token rows are scoped to the address that was asked about
---------------------------------------------------------
``tokentx?address=A`` means transfers where A is sender or recipient, and the
adapter is written against that. A transaction page is not scoped that way: it
lists every transfer in the transaction. The airdrop that reached the case
study address carries **500** ERC-20 transfers between 501 addresses, one of
which is ours. Emitting all of them would push 500 unrelated addresses into
the frontier out of a single piece of spam. So a token row is emitted only for
a transfer with the requested address at one end.

That filter is allowed to remove rows but never all of them: the address's own
ERC-20 pane is what named this transaction, so a section holding transfers of
which NONE touch the address is the two pages contradicting each other, and
that raises. Dropping the transaction quietly would be a hole in a feed that
still looked complete.

Reading defensively, because HTML changes without notice
--------------------------------------------------------
Zero rows from this tier reads as *this address has no history*, which is the
worst answer this system can give. So emptiness must be **proved**, never
inferred from a failed match: every parse asserts the structural markers it
depends on and raises ``ProviderResponseInvalid`` when they are missing. An
error is recoverable — the pool fails over and the run records it. A false
empty ends a trace with a wrong answer that nothing downstream can detect.

Proving it on an address page needs one specific fact about 3xpl's layout,
measured across five recorded pages rather than guessed. The page renders
**seven tab panes** — one per event module, plus a "recent transactions"
pseudo-tab — and only the pane for the module in the URL is real. Every other
pane is a placeholder, and a placeholder renders as an *empty table* on some
pages and as a "there are no events in this module" *notice* on others. So a
parser that counts empty markers across the whole page finds six of them on a
page holding ten transactions, and would report an address with history as
having none the moment it stopped recognising a row. The page names its own
active pane — a ``checked`` radio in the tab strip, in pane order — and that
is what this module keys on:

- the requested module's pane, or an error. Never "the first table", never
  "the last table": the recorded pages carry 1, 2 and 7 tables, and the real
  one sits at a different index on each. It also moves with the module — the
  native pane is index 1 and the ERC-20 pane is index 3 on the same address —
  which is exactly why the index is read off the page and never hard-coded.
- rows in that pane are the answer; an empty marker or a "no events in this
  module" notice **in that pane** is a proven empty; anything else raises.
- and the tab strip must still INDEX the panes: the checked radio gives an
  ordinal, which addresses the right pane only while radios and panes stay
  one-to-one. One extra ``tab-content`` div above the strip shifts every pane
  after it onto a placeholder, and placeholders are exactly the thing that
  always looks empty — measured, the recorded ten-transaction page came back a
  proven empty with nothing raised. So the two counts are compared, and a
  disagreement is an error rather than a guess about whose history this is.

A section that EXISTS gets the same treatment as one that is missing: it has
to be about the thing that was asked for. A ``Main events`` section holding
nothing but a token leg is not native history, so emitting no row for it would
drop a transaction the address's own pane had just listed — silently, and
without any of it being wrong enough to notice. The same contradiction raises
on the token side, and it raises here, symmetrically. It matters more here:
``ADDRESS_HISTORY`` is the feed that must not degrade, so a feed emptied this
way arrives as "this address never transacted".

The same discipline applies one level down, on the transaction page. A
transaction with no events of some kind does not render an empty section — it
collapses them into an "Additional events" notice, and that notice NAMES the
kinds it is talking about: "There are no events of Internal, ERC-721 and
ERC-1155 types." Accepting the mere presence of that section as proof left a
hole exactly the width of a renamed heading: rename "ERC-20 events" and every
token fetch finds no section, sees an "Additional events" notice about
something else entirely, and returns zero rows for every transaction on the
address — a whole feed emptied without one error to show for it. So the notice
has to name the module that was asked about, or it is not an answer about it.

What survives is one residual: 3xpl rendering the requested pane as an empty
table while the module in fact has events. That is the site answering wrongly,
not a parse this module can second-guess.
"""

from __future__ import annotations

import asyncio
import logging
import re
import time
from abc import ABC, abstractmethod
from collections import OrderedDict
from collections.abc import Awaitable, Callable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime
from html.parser import HTMLParser
from typing import Any, ClassVar
from urllib.parse import urlsplit
from urllib.robotparser import RobotFileParser

import httpx

from cipherchain.core.errors import ProviderResponseInvalid
from cipherchain.core.hashing import canonical_json_bytes, sha256_hex
from cipherchain.core.models import Capability
from cipherchain.providers.base import (
    SHORT_READ_KEY,
    Provider,
    ProviderRequest,
    ProviderResponse,
)
from cipherchain.providers.clients._http import perform
from cipherchain.providers.ratelimit import TokenBucket

logger = logging.getLogger(__name__)

# Identify the tool and say what it is doing. A tier forbidden from
# impersonating a browser has to be attributable instead: a site operator
# reading their logs can tell exactly who this is and block it deliberately.
DEFAULT_USER_AGENT = (
    "CipherChain-investigation/0.1 (+blockchain investigation research; public pages only)"
)

# One request per five seconds, and the number is measured, not chosen for
# politeness. 3xpl states its own budget on every 200 response:
#
#     x-ratelimit-limit: 25
#     x-ratelimit-remaining: 24
#
# 25 requests per minute is 0.42/s, but the published number is not the whole
# rule: measured on 2026-08-16, a second request 1.5s after a successful one
# was refused, while a 5s gap was served with the budget reported full again.
# So there is a short-window guard underneath the per-minute figure, and 5s is
# the interval actually observed to work. 12 requests a minute against a
# published 25 is the margin, and it is deliberately generous, because
# exceeding it is not a throttle — it is an escalation:
#
#   1. over budget → HTTP 429 whose body is a full "Verify you are not a robot"
#      interstitial (56 KB of HTML, not an error page),
#   2. keep going → HTTP 403 with ``Retry-After: 3600`` and an 80-byte refusal.
#      One hour, for the whole host, for every chain and every capability.
#
# This default WAS 1.0/s. That is 60 requests a minute against a stated 25, and
# it earned exactly the ban above within about twenty requests — so the tier
# whose entire purpose is to keep working after the keyed quotas are spent was
# reliably destroying its own access on the first address it read. A one-hour
# blackout is not a slow trace; it is the stopped trace this tier exists to
# prevent, and it is self-inflicted. The instruction this tier was built from
# was "idc if it takes time", which is exactly the licence to be this slow.
#
# The rate stays a constructor argument so an operator can hold a stricter line,
# and robots' own Crawl-delay overrides both when it is slower. Whatever the
# number, it stays far below any keyed provider's allowance, which is what keeps
# this tier the last one reached.
DEFAULT_RATE_PER_SEC = 0.2

# What the site calls its own budget. Read only to WARN — a tier that quietly
# steered by a vendor header would hide the moment the header changed meaning.
_RATELIMIT_REMAINING = "x-ratelimit-remaining"

_ASSET_HREF = re.compile(r"/asset/([^\"?#]+)")
_BLOCK_HREF = re.compile(r"/block/(\d+)\b")
_DECIMAL = re.compile(r"^\d+(\.\d+)?$")

# An unverified token has no asset LINK — 3xpl renders its name as a bare
# `<span>` and states the asset id only in the hover tooltip. So the tooltip is
# the fallback, and it is not an exotic one: the only ERC-20 event on the case
# study address is an unverified token, and reading the id from the href alone
# made that transaction unparseable for EVERY capability, native included.
_TOOLTIP_ID = re.compile(r"Id:\s*(\S+)")
_TOOLTIP_NAME = re.compile(r"Name:\s*(.+)")

# Section headings on a 3xpl transaction page. "Main events" is the top-level
# value transfer, on every chain the site serves; "Internal events" is the
# trace-delivered value that `txlist` never shows; the token heading names the
# token standard, so it is per-chain and lives on the dialect. A transaction
# missing any of them collapses those into an "Additional events" notice — which
# is a PROVEN empty rather than a parse miss, and that distinction is the whole
# reason the heading is tracked at all.
_SECTION_MAIN = "Main events"
_SECTION_INTERNAL = "Internal events"
_SECTION_TOKENS = "ERC-20 events"
_EMPTY_SECTION = "Additional events"

# Headings on a 3xpl asset page. "Decimals" is the fact this module cannot
# derive and will not guess; "ID" is read back so a page served for some other
# asset cannot supply a scale for this one.
_ASSET_DECIMALS = "Decimals"
_ASSET_ID = "ID"


def _notice_names(notice: str, module: str) -> bool:
    """Does an "Additional events" notice name this module as absent?

    Word-bounded, so "ERC-20" does not match inside "ERC-2000" and a notice
    about neighbouring modules cannot pass for an answer about this one.
    """
    return re.search(rf"(?<![\w-]){re.escape(module)}(?![\w-])", notice) is not None


# ── row dialects ─────────────────────────────────────────────────────────


@dataclass(frozen=True, slots=True)
class _Transfer:
    sender: str
    recipient: str
    amount: str
    asset_id: str
    symbol: str
    name: str | None = None


@dataclass(frozen=True, slots=True)
class _Stamp:
    """What every row read off one transaction page shares."""

    tx_hash: str
    block_number: int
    timestamp_ms: int


class RowEmitter(ABC):
    """One chain's row dialect: how its pages spell identifiers, and the body
    its adapter reads.

    There are two because there are two adapters and they do not read the same
    thing. ``EvmAdapter`` reads Etherscan's rows — a bare list of
    ``hash``/``from``/``to``, ``0x``-prefixed and lower-cased. ``TronAdapter``
    reads TronGrid's envelope — ``{"data": [...]}`` whose rows carry ``txID``,
    ``raw_data.contract[]`` and Base58 addresses. Handing either one the other's
    shape is not a degraded answer, it is an exception inside the adapter:
    ``.get("data")`` on a list.

    The identifier patterns belong here for the same reason the rows do. A Tron
    transaction id has no ``0x``, and a Tron address is Base58 where case is
    part of the value — so the EVM patterns match nothing on a Tron page, and
    nothing is the answer this tier is least allowed to give.
    """

    #: how this chain's hrefs spell a transaction and an address
    tx_href: ClassVar[re.Pattern[str]]
    address_href: ClassVar[re.Pattern[str]]
    #: the contract half of a token asset id ("<module>/<contract>")
    token_contract: ClassVar[re.Pattern[str]]
    #: which transaction-page section answers each capability
    sections: ClassVar[Mapping[Capability, str]]
    #: what the "Additional events" notice calls each module when it is empty
    empty_types: ClassVar[Mapping[Capability, str]]
    #: whether the caller can ask for the next page by NUMBER. False means its
    #: cursor is somebody else's opaque token, which this tier cannot mint and
    #: cannot position a page from — see :meth:`envelope`.
    caller_pages_by_number: ClassVar[bool]

    @abstractmethod
    def canonical(self, address: str) -> str:
        """The address as this chain writes it, for URLs and for comparison."""

    @abstractmethod
    def native_row(self, stamp: _Stamp, transfer: _Transfer, *, amount: int) -> dict[str, Any]:
        """One native-value row, in the shape this chain's adapter reads."""

    @abstractmethod
    def token_row(
        self,
        stamp: _Stamp,
        transfer: _Transfer,
        *,
        contract: str,
        decimals: int,
        amount: int,
    ) -> dict[str, Any]:
        """One token-transfer row, in the shape this chain's adapter reads."""

    @abstractmethod
    def envelope(self, rows: list[dict[str, Any]], *, truncated: bool) -> Any:
        """The payload around the rows — a page, as the adapter unwraps one.

        ``truncated`` is the short-read mark from :meth:`_enumerate`, and an
        envelope that cannot carry it must be one whose ``caller_pages_by_number``
        is True — that flag is the only way ``truncated`` is ever set.
        """


class _EtherscanRows(RowEmitter):
    """``txlist`` / ``txlistinternal`` / ``tokentx`` rows, bare, as
    ``EvmAdapter`` reads them from every other provider in the pool."""

    tx_href = re.compile(r"/transaction/(0x[0-9a-fA-F]{64})\b")
    address_href = re.compile(r"/address/(0x[0-9a-fA-F]{40})\b")
    token_contract = re.compile(r"0x[0-9a-fA-F]{40}")
    sections: ClassVar[Mapping[Capability, str]] = {
        Capability.ADDRESS_HISTORY: _SECTION_MAIN,
        Capability.TOKEN_TRANSFERS: _SECTION_TOKENS,
        Capability.INTERNAL_TRACES: _SECTION_INTERNAL,
    }
    # "Main" never appears in a recorded notice because 3xpl always renders a
    # Main events section — it prints a zero transfer ("0.000000000000000000
    # ETH") rather than omitting it — so for native history a missing section is
    # simply an error, which is what this mapping produces.
    empty_types: ClassVar[Mapping[Capability, str]] = {
        Capability.ADDRESS_HISTORY: "Main",
        Capability.TOKEN_TRANSFERS: "ERC-20",
        Capability.INTERNAL_TRACES: "Internal",
    }
    caller_pages_by_number = True

    def canonical(self, address: str) -> str:
        return address.lower()

    def native_row(self, stamp: _Stamp, transfer: _Transfer, *, amount: int) -> dict[str, Any]:
        return {
            "blockNumber": str(stamp.block_number),
            "timeStamp": str(stamp.timestamp_ms // 1000),
            "hash": stamp.tx_hash,
            "from": transfer.sender,
            "to": transfer.recipient,
            "value": str(amount),
            # Only transfers the page renders as having HAPPENED reach here — a
            # reverted call contributes no events — so every row emitted is a
            # successful one. No traceId is set on purpose: the caller's
            # fallback key is the transfer's own content, which matches across
            # providers, whereas a vendor-local trace index would not.
            "isError": "0",
        }

    def token_row(
        self,
        stamp: _Stamp,
        transfer: _Transfer,
        *,
        contract: str,
        decimals: int,
        amount: int,
    ) -> dict[str, Any]:
        row: dict[str, Any] = {
            "blockNumber": str(stamp.block_number),
            "timeStamp": str(stamp.timestamp_ms // 1000),
            "hash": stamp.tx_hash,
            "from": transfer.sender,
            "to": transfer.recipient,
            "value": str(amount),
            "contractAddress": contract.lower(),
            "tokenSymbol": transfer.symbol,
            "tokenDecimal": str(decimals),
        }
        if transfer.name is not None:
            row["tokenName"] = transfer.name
        return row

    def envelope(self, rows: list[dict[str, Any]], *, truncated: bool) -> Any:
        # A bare list has nowhere to put the short-read mark. Safe only because
        # `caller_pages_by_number` is True here — `_enumerate` sets `truncated`
        # for an emitter whose caller pages by somebody else's opaque cursor and
        # for no other. Asserted rather than assumed: a future emitter that
        # flips that flag and keeps a bare list would drop the mark silently,
        # and a short read that says nothing is the exact failure this whole
        # path exists to prevent.
        assert not truncated, "a bare row list cannot carry a short-read mark"
        return rows


class _TronGridRows(RowEmitter):
    """The body ``TronAdapter`` reads: TronGrid's ``{"data": [...]}`` envelope,
    with native rows keyed ``txID`` and token rows keyed ``transaction_id``.

    Shapes are matched against the recorded TronGrid payloads in
    ``tests/chains/fixtures/tron_native.json`` and ``tron_trc20.json``, which are
    the same bytes the adapter was written against.
    """

    # No `0x`: a Tron transaction id is 64 bare hex characters, so the EVM
    # pattern would match nothing here and every Tron address page would parse
    # as a pane holding no rows.
    tx_href = re.compile(r"/transaction/([0-9a-fA-F]{64})\b")
    address_href = re.compile(r"/address/(T[1-9A-HJ-NP-Za-km-z]{33})\b")
    token_contract = re.compile(r"T[1-9A-HJ-NP-Za-km-z]{33}")
    # Unlike the module slugs, these two strings were NOT read off a Tron page —
    # no 3xpl Tron page has been recorded. They follow the Ethereum layout with
    # the token standard substituted, and the substitution is safe only because
    # getting it wrong RAISES: an unrecognised heading is a missing section, and
    # a missing section without an "Additional events" notice naming the module
    # is `_rows_for`'s error, not zero rows. The dangerous version of this
    # mistake — the one where a renamed heading empties a whole feed silently —
    # is the one `_notice_names` exists to prevent, and it applies here too.
    sections: ClassVar[Mapping[Capability, str]] = {
        Capability.ADDRESS_HISTORY: _SECTION_MAIN,
        Capability.TOKEN_TRANSFERS: "TRC-20 events",
    }
    empty_types: ClassVar[Mapping[Capability, str]] = {
        Capability.ADDRESS_HISTORY: "Main",
        Capability.TOKEN_TRANSFERS: "TRC-20",
    }
    # TronGrid pages on an opaque `meta.fingerprint` and the adapter passes it
    # straight back down. Nothing on a numbered page can produce one.
    caller_pages_by_number = False

    def canonical(self, address: str) -> str:
        # NOT lower-cased. Base58Check is case-significant, so folding a Tron
        # address changes it into one that fails its own checksum: the URL 404s
        # and the "is this transfer mine" scope test in `_token_rows` matches
        # nothing, which reads as the two pages contradicting each other.
        return address

    def native_row(self, stamp: _Stamp, transfer: _Transfer, *, amount: int) -> dict[str, Any]:
        return {
            "txID": stamp.tx_hash,
            "blockNumber": stamp.block_number,
            "block_timestamp": stamp.timestamp_ms,
            # `TronAdapter._succeeded` drops the ENTIRE native leg unless some
            # `ret` entry says SUCCESS, and an absent `ret` is read as failure.
            # The claim behind the stamp is the same one the EVM row's
            # `isError: "0"` rests on: 3xpl renders events, and a transfer that
            # did not happen has none to render.
            "ret": [{"contractRet": "SUCCESS"}],
            "raw_data": {
                "contract": [
                    {
                        "type": "TransferContract",
                        "parameter": {
                            "value": {
                                "amount": amount,
                                # Base58 as the page prints it. The adapter's
                                # `_address` passes a `T…` through untouched and
                                # only converts the `41…` hex that raw TronGrid
                                # bodies use, so emitting the canonical form
                                # here is what keeps one wallet one node.
                                "owner_address": transfer.sender,
                                "to_address": transfer.recipient,
                            }
                        },
                    }
                ]
            },
        }

    def token_row(
        self,
        stamp: _Stamp,
        transfer: _Transfer,
        *,
        contract: str,
        decimals: int,
        amount: int,
    ) -> dict[str, Any]:
        info: dict[str, Any] = {
            "symbol": transfer.symbol,
            "address": contract,
            "decimals": decimals,
        }
        if transfer.name is not None:
            info["name"] = transfer.name
        return {
            "transaction_id": stamp.tx_hash,
            "block_timestamp": stamp.timestamp_ms,
            "from": transfer.sender,
            "to": transfer.recipient,
            "type": "Transfer",
            # A string, like TronGrid's own: the adapter reads it through
            # `int()`, so the type is free, and matching the vendor keeps one
            # dialect rather than two that differ only where nobody looks.
            "value": str(amount),
            "token_info": info,
        }

    def envelope(self, rows: list[dict[str, Any]], *, truncated: bool) -> Any:
        # No `meta`. `TronAdapter._feed_fingerprint` keys on `meta.links.next`
        # and reads its absence as "this feed is finished" — which is exactly
        # true of a page this tier cannot continue. Minting a fingerprint would
        # send the adapter back for a page nobody can position, and it would
        # come back here as a decline; the honest stop costs the rows past the
        # pages-per-call bound.
        body: dict[str, Any] = {"data": rows}
        if truncated:
            # The one key here TronGrid never sends, and it is deliberate. "This
            # feed is finished" and "this feed was cut and cannot be resumed"
            # are indistinguishable in TronGrid's own dialect, because TronGrid
            # can always mint the next fingerprint and this tier cannot. Left
            # unsaid, the adapter builds a page with no cursor and no gap, the
            # engine's two `mark_history_truncated` triggers both miss, and an
            # address read 10 explorer pages deep is counted as read in FULL —
            # the report's "N address(es) had more history than was read" then
            # omits exactly the busy addresses where a VASP is likeliest.
            body[SHORT_READ_KEY] = True
        return body


ETHERSCAN_ROWS: RowEmitter = _EtherscanRows()
TRONGRID_ROWS: RowEmitter = _TronGridRows()


@dataclass(frozen=True, slots=True)
class ExplorerSite:
    """One explorer, one chain. Adding a chain is a table edit rather than
    code — but only after someone has loaded the pages and checked that the
    module ids and the native asset id are what this file assumes."""

    chain: str
    base_url: str
    path_chain: str  # the explorer's own slug for this chain
    native_asset_id: str  # the asset id its currency links use for native value
    native_decimals: int
    modules: Mapping[Capability, str]
    # The dialect this chain's ADAPTER reads, which is not a property of the
    # explorer at all. It defaults to Etherscan's because that is what every EVM
    # chain's adapter reads; a chain whose adapter reads something else has to
    # say so here, or the pages parse fine and the adapter raises on the result.
    rows: RowEmitter = ETHERSCAN_ROWS

    @property
    def host(self) -> str:
        return urlsplit(self.base_url).netloc

    def address_path(self, address: str, capability: Capability, site_page: int) -> str:
        return f"/{self.path_chain}/address/{address}/{self.modules[capability]}/events/{site_page}"

    def transaction_path(self, tx_hash: str) -> str:
        return f"/{self.path_chain}/transaction/{tx_hash}"

    def asset_path(self, asset_id: str) -> str:
        # Asset pages are NOT under the chain prefix — the asset id already
        # carries its chain ("ethereum-erc-20/0xa0b8…"), so prefixing again
        # gives a 404 and a parse error on every token row.
        return f"/asset/{asset_id}"


# Ethereum and Tron, deliberately: the module ids below were read off live pages
# on 2026-08-16. Polygon's slugs were NOT checked, and a guessed slug would 404
# into a parse error on every call — a chain missing from this table is honest,
# a chain present on a guess is not.
DEFAULT_SITES: Mapping[str, ExplorerSite] = {
    "ethereum": ExplorerSite(
        chain="ethereum",
        base_url="https://3xpl.com",
        path_chain="ethereum",
        native_asset_id="ethereum",
        native_decimals=18,
        modules={
            Capability.ADDRESS_HISTORY: "ethereum-main",
            Capability.TOKEN_TRANSFERS: "ethereum-erc-20",
            Capability.INTERNAL_TRACES: "ethereum-trace",
        },
    ),
    # Tron reaches this tier through `rows`, not through the slugs — see "The
    # second chain, and the second row dialect it needed" above. Three fields
    # here are worth naming individually, because they were established
    # differently:
    #
    # - the module slugs are read off a live page (2026-08-16), like Ethereum's.
    #   `tron-internal` and `tron-trc-10` are verified too and still unmapped:
    #   TronAdapter asks for neither, so a row emitted for them would reach no
    #   consumer and be checked against no vendor body.
    # - `native_decimals` is TRX's 6, which is `chains.tron.TRX_ASSET.decimals`
    #   — the same constant the adapter builds every TRX movement with, so a
    #   disagreement here would be a disagreement with the chain SDK.
    # - `native_asset_id` is NOT read off a live page. It follows 3xpl's own
    #   pattern, where the native asset id is the chain slug ("ethereum"), and
    #   nothing weaker than a Tron page will confirm it. It fails LOUDLY if it
    #   is wrong: `_rows_for` finds no transfer in that asset and raises "the
    #   two pages disagree about what it moved" rather than emitting a row.
    "tron": ExplorerSite(
        chain="tron",
        base_url="https://3xpl.com",
        path_chain="tron",
        native_asset_id="tron",
        native_decimals=6,
        modules={
            Capability.ADDRESS_HISTORY: "tron-main",
            Capability.TOKEN_TRANSFERS: "tron-trc-20",
        },
        rows=TRONGRID_ROWS,
    ),
}


# ── robots ───────────────────────────────────────────────────────────────


@dataclass(frozen=True, slots=True)
class RobotsDecision:
    allowed: bool
    reason: str
    crawl_delay: float | None = None


class RobotsPolicy:
    """Fetch, parse and cache one ``robots.txt`` per host.

    Fails CLOSED. A 5xx, a transport error, a redirect or an outright refusal
    means we do not know what the site permits, and "we could not read the
    rules" must never resolve to "so we crawled anyway" — RFC 9309 §2.3.1.4
    takes the same position. A 404 is different: it is the site answering that
    it publishes no rules, which is the documented allow-all. Etherscan is the
    live redirect case, and declining it is why this class exists at all.

    Decisions are cached for the process, because re-reading robots before
    every page would itself be the impolite behaviour the file prevents.
    """

    def __init__(
        self,
        http: httpx.AsyncClient,
        *,
        user_agent: str = DEFAULT_USER_AGENT,
    ) -> None:
        self._http = http
        self._user_agent = user_agent
        self._cache: dict[str, RobotFileParser | RobotsDecision] = {}
        self._lock = asyncio.Lock()

    async def decide(self, base_url: str, path: str) -> RobotsDecision:
        host = urlsplit(base_url).netloc
        async with self._lock:
            entry = self._cache.get(host)
            if entry is None:
                entry = await self._load(base_url)
                self._cache[host] = entry
        if isinstance(entry, RobotsDecision):
            return entry
        if not entry.can_fetch(self._user_agent, path):
            return RobotsDecision(False, f"robots.txt on {host} disallows {path}")
        delay = entry.crawl_delay(self._user_agent)
        return RobotsDecision(
            True,
            f"robots.txt on {host} allows {path}",
            crawl_delay=float(delay) if delay is not None else None,
        )

    async def _load(self, base_url: str) -> RobotFileParser | RobotsDecision:
        url = f"{base_url.rstrip('/')}/robots.txt"
        try:
            response = await self._http.get(url, headers={"User-Agent": self._user_agent})
        except httpx.HTTPError as exc:
            return RobotsDecision(False, f"robots.txt unreachable ({exc!r})")
        if response.status_code in (404, 410):
            allow_all = RobotFileParser()
            allow_all.parse([])  # no rules published: the documented allow-all
            return allow_all
        if response.status_code >= 300:
            return RobotsDecision(
                False, f"robots.txt answered HTTP {response.status_code} — rules unknown"
            )
        parser = RobotFileParser()
        parser.parse(response.text.splitlines())
        return parser


# ── page parsing ─────────────────────────────────────────────────────────


def _classes(attrs: Sequence[tuple[str, str | None]]) -> frozenset[str]:
    for name, value in attrs:
        if name == "class" and value:
            return frozenset(value.split())
    return frozenset()


def _attr(attrs: Sequence[tuple[str, str | None]], name: str) -> str | None:
    for key, value in attrs:
        if key == name:
            return value
    return None


def _has_attr(attrs: Sequence[tuple[str, str | None]], name: str) -> bool:
    """Presence, not value. The block link marks itself with a BARE ``data-block``
    attribute, which HTMLParser reports as a ``None`` value — testing the value
    instead silently found no block on any page, and rows without a height are
    then rejected as unreadable. Caught by parsing a recorded live page."""
    return any(key == name for key, _ in attrs)


@dataclass
class _Pane:
    """One tab pane, read in isolation from its neighbours.

    Isolation is the point. Six of the seven panes on an address page are
    placeholders for modules nobody asked about, and a placeholder always looks
    empty — so an empty marker only means anything when it is known to belong
    to the pane for the module in the URL.
    """

    transactions: list[str] = field(default_factory=list)
    rows: int = 0  # tx links seen INCLUDING repeats: the site's page size
    header_cells: list[str] = field(default_factory=list)
    empty_markers: int = 0
    notice: bool = False

    @property
    def names_transactions(self) -> bool:
        return any("transaction" in cell.lower() for cell in self.header_cells)

    @property
    def proves_empty(self) -> bool:
        """The two shapes 3xpl uses to say "nothing here", and only those.

        An empty table (a module with events, on a page past its last) has to
        carry the events header too — an empty table of something else is not
        an answer about this module's history.
        """
        return (self.empty_markers > 0 and self.names_transactions) or self.notice


class _AddressPageParser(HTMLParser):
    """Every tab pane on one address/module page, plus which one is active.

    The active pane is the one whose radio in the tab strip carries ``checked``;
    the radios appear in pane order, so the ordinal is the index. Requesting
    ``/ethereum-trace/events/0`` checks the second module radio and fills the
    third pane — measured, which is why the index is read off the page instead
    of assumed.
    """

    _NOTICE = "no events in this module"

    def __init__(self, rows: RowEmitter) -> None:
        super().__init__(convert_charrefs=True)
        self._rows = rows
        self.panes: list[_Pane] = []
        self.active_index: int | None = None
        self._radios = 0
        self._div_depth = 0
        self._pane: _Pane | None = None
        self._pane_depth = 0
        self._in_head_cell = False
        self._body_depth = 0

    @property
    def tabs(self) -> int:
        """How many module tabs the strip declares. See :attr:`active`."""
        return self._radios

    @property
    def active(self) -> _Pane | None:
        """The pane for the module that was asked for, or ``None`` when the
        page does not say — and not saying is an error, never an empty."""
        if self.active_index is None:
            return None
        # The index is an ORDINAL among the tab-strip radios, and it addresses
        # the pane list only while the two stay one-to-one. That correspondence
        # is an assumption about someone else's markup, so it is checked rather
        # than trusted: ONE extra `tab-content` div anywhere above the strip — a
        # promo card, a sidebar widget, a pane nested inside another — shifts
        # every pane after it, and the pane read then belongs to a module nobody
        # asked about.
        #
        # That is the one failure mode this file is least allowed to have.
        # Placeholder panes always look empty, so the result is not a parse
        # error that fails over: it is a populated page reporting a PROVEN
        # empty. Measured against the recorded 10-transaction page, a single
        # injected div turns it into "this address has no history" with nothing
        # raised anywhere. Refusing to guess costs a recoverable error on a page
        # we could perhaps still have read; accepting the guess costs a wrong
        # answer nothing downstream can detect.
        if len(self.panes) != self._radios:
            return None
        if self.active_index >= len(self.panes):
            return None
        return self.panes[self.active_index]

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        classes = _classes(attrs)
        if tag == "input" and "tabs__radio-button" in classes:
            if _has_attr(attrs, "checked"):
                self.active_index = self._radios
            self._radios += 1
            return
        if tag == "div":
            self._div_depth += 1
            # Panes are siblings, never nested, so the first `tab-content` at
            # any depth opens one and the rest of that subtree belongs to it.
            if self._pane is None and "tab-content" in classes:
                self._pane = _Pane()
                self._pane_depth = self._div_depth
                self.panes.append(self._pane)
            return
        if self._pane is None:
            return  # outside every pane: page furniture, not an answer
        if tag == "th":
            self._in_head_cell = True
        elif tag == "tbody":
            self._body_depth += 1
        elif tag == "td" and "table__cell_empty" in classes:
            self._pane.empty_markers += 1
        elif tag == "a" and self._body_depth:
            match = self._rows.tx_href.search(_attr(attrs, "href") or "")
            if match is not None:
                self._pane.rows += 1
                tx_hash = match.group(1).lower()
                if tx_hash not in self._pane.transactions:
                    self._pane.transactions.append(tx_hash)

    def handle_endtag(self, tag: str) -> None:
        if tag == "div":
            if self._pane is not None and self._div_depth <= self._pane_depth:
                self._pane = None
                self._body_depth = 0
                self._in_head_cell = False
            self._div_depth = max(0, self._div_depth - 1)
        elif tag == "th":
            self._in_head_cell = False
        elif tag == "tbody" and self._body_depth:
            self._body_depth -= 1

    def handle_data(self, data: str) -> None:
        if self._pane is None:
            return
        text = data.strip()
        if not text:
            return
        if self._in_head_cell:
            self._pane.header_cells.append(text)
        elif self._NOTICE in text.lower():
            self._pane.notice = True


@dataclass
class _PendingTransfer:
    addresses: list[str] = field(default_factory=list)
    amount: str | None = None
    asset_id: str | None = None
    symbol: str | None = None
    name: str | None = None


class _TransactionPageParser(HTMLParser):
    """Transfers, block height and timestamp from one transaction page.

    Transfers are grouped under the section heading above them, because the
    heading is what says whether a value move was the top-level call or a
    trace — and those are two different capabilities. ``sections`` records
    every heading seen, including the empty ones, so a caller can distinguish
    "this transaction had no internal events" from "we never found the
    section".
    """

    def __init__(self, rows: RowEmitter) -> None:
        super().__init__(convert_charrefs=True)
        self._rows = rows
        self.sections: dict[str, list[_Transfer]] = {}
        # The text of the "Additional events" notice, verbatim. It names which
        # modules are empty, and a caller that only checked the section existed
        # would accept a notice about ERC-721 as proof that ERC-20 is empty.
        #
        # Read from the notice ELEMENT (`notice__text`), not from "everything
        # printed after the heading". The heading gate alone runs until the next
        # `<h1>`, and on a page where "Additional events" is the last heading
        # that swallows the footer and the inline `<script>` bodies after it —
        # measured: a footer link reading "ERC-20 tokens" lands in this string.
        # That matters because this text is the ONLY thing standing between a
        # renamed section heading and a whole token feed reporting zero rows
        # with no error anywhere. Polluted, it says "yes, ERC-20 is empty" about
        # a transaction whose ERC-20 section is sitting right there under a name
        # we no longer recognise. The recorded pages only survive that by
        # accident: a "Special data" heading happens to follow and close the
        # accumulation off. Bound to the element, the accident is not load-bearing.
        self.empty_notice = ""
        self.block_number: int | None = None
        self.timestamp_ms: int | None = None
        self.partial = 0
        self._heading = ""
        self._in_heading = False
        self._depth = 0
        self._transfer_depth: int | None = None
        self._addresses_depth: int | None = None
        self._values_depth: int | None = None
        self._pending: _PendingTransfer | None = None
        self._value_paragraph = False
        # All three hold text and nothing else, so they end at the next end tag
        # of any name — no depth bookkeeping, and no way for one to outlive its
        # element and swallow the next transfer's currency, or the page footer.
        self._in_currency_name = False
        self._in_tooltip = False
        self._in_notice_text = False

    # A transaction page never nests one transfer inside another, so one depth
    # marker per region is enough to know where each region ends.
    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag == "h1":
            self._in_heading = True
            self._heading = ""
            return
        classes = _classes(attrs)
        started = _attr(attrs, "data-start-timestamp")
        if started is not None and started.isdigit() and self.timestamp_ms is None:
            self.timestamp_ms = int(started)
        if "notice__text" in classes and self._heading == _EMPTY_SECTION:
            # The notice is read from its OWN element, never from "all text
            # after the heading". Both conditions are required: the class alone
            # would pick up notices belonging to other sections, and the heading
            # alone runs until the next `<h1>` and so swallows whatever trails
            # the page. See `empty_notice` for what that costs.
            self._in_notice_text = True
        if self._value_paragraph and self._pending is not None:
            # The currency block inside a value paragraph: its name carries the
            # symbol, its tooltip carries "Id: …" / "Name: …". A verified token
            # links its name, an unverified one does not — the tooltip is the
            # only place BOTH shapes state the asset id.
            if "currency__name" in classes:
                self._in_currency_name = True
            elif "tooltip" in classes:
                self._in_tooltip = True
        if tag == "a":
            self._anchor(attrs, classes)
        elif tag == "div":
            self._depth += 1
            if "even-transfer" in classes and self._transfer_depth is None:
                self._transfer_depth = self._depth
                self._pending = _PendingTransfer()
            elif "even-transfer__addresses-row" in classes:
                self._addresses_depth = self._depth
            elif "even-transfer__values" in classes:
                self._values_depth = self._depth
        elif tag == "p" and self._values_depth is not None and self._pending is not None:
            # Inside a values block the paragraphs run: caption, amount,
            # separator, fiat estimate. Only the amount is a fact about the
            # chain — the fiat figure is a price quote and must never be read
            # as a value.
            annotation = {"even-transfer-values__caption", "even-transfer-values__delimiter"}
            self._value_paragraph = not (classes & annotation) and self._pending.amount is None

    def _anchor(self, attrs: Sequence[tuple[str, str | None]], classes: frozenset[str]) -> None:
        href = _attr(attrs, "href") or ""
        if _has_attr(attrs, "data-block"):
            match = _BLOCK_HREF.search(href)
            if match is not None and self.block_number is None:
                self.block_number = int(match.group(1))
        if self._pending is None:
            return
        if self._addresses_depth is not None and "hash__hash" in classes:
            match = self._rows.address_href.search(href)
            if match is not None:
                self._pending.addresses.append(self._rows.canonical(match.group(1)))
        elif self._value_paragraph and "currency__name" in classes:
            match = _ASSET_HREF.search(href)
            if match is not None and self._pending.asset_id is None:
                self._pending.asset_id = match.group(1)

    def handle_endtag(self, tag: str) -> None:
        self._in_currency_name = False
        self._in_tooltip = False
        self._in_notice_text = False
        if tag == "h1":
            self._in_heading = False
            self.sections.setdefault(self._heading, [])
        elif tag == "p":
            self._value_paragraph = False
        elif tag == "div":
            if self._addresses_depth == self._depth:
                self._addresses_depth = None
            if self._values_depth == self._depth:
                self._values_depth = None
            if self._transfer_depth == self._depth:
                self._transfer_depth = None
                self._finish()
            self._depth -= 1

    def handle_data(self, data: str) -> None:
        if self._in_heading:
            self._heading = (self._heading + data).strip()
            return
        text = data.strip()
        if not text:
            return
        if self._pending is not None and self._in_currency_name:
            if self._pending.symbol is None:
                self._pending.symbol = text
        elif self._pending is not None and self._in_tooltip:
            self._read_tooltip(data)
        elif self._value_paragraph and self._pending is not None and self._pending.amount is None:
            self._pending.amount = text
        elif self._in_notice_text:
            self.empty_notice = f"{self.empty_notice} {text}".strip()

    def _read_tooltip(self, data: str) -> None:
        """A currency tooltip reads "Id: <asset id>\\nName: <display name>".

        There are two tooltips in a currency block — the other one just says
        "Verified currency" — so the id is taken from whichever one states it
        rather than from whichever comes first.
        """
        pending = self._pending
        if pending is None:
            return
        found = _TOOLTIP_ID.search(data)
        if found is not None and pending.asset_id is None:
            pending.asset_id = found.group(1)
        named = _TOOLTIP_NAME.search(data)
        if named is not None and pending.name is None:
            pending.name = named.group(1).strip()

    def _finish(self) -> None:
        pending, self._pending = self._pending, None
        if pending is None:
            return
        # A transfer we cannot read exactly is counted, never repaired by
        # assumption. `partial` is what the caller turns into an error rather
        # than into a quietly shorter list of movements. The symbol counts too:
        # `assets` keys on (chain, kind, contract) with the symbol OUTSIDE the
        # key, so a row that fell back to a placeholder symbol would rename that
        # contract for every row a keyed provider ever wrote.
        if (
            len(pending.addresses) != 2
            or pending.amount is None
            or pending.asset_id is None
            or pending.symbol is None
        ):
            self.partial += 1
            return
        self.sections.setdefault(self._heading, []).append(
            _Transfer(
                sender=pending.addresses[0],
                recipient=pending.addresses[1],
                amount=pending.amount,
                asset_id=pending.asset_id,
                symbol=pending.symbol,
                name=pending.name,
            )
        )


class _AssetPageParser(HTMLParser):
    """The stated facts on one 3xpl asset page, by heading.

    The page is a list of ``currency-section`` blocks, each a ``section-title``
    heading and a value: "ID" → ``ethereum-erc-20/0xa0b8…``, "Decimals" → 6.
    Only the headings are keyed on, because the value markup differs between
    sections (the id sits in a div beside an SVG, the decimals in a plain
    paragraph) and keying on either class would read one and miss the other.
    """

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.values: dict[str, str] = {}
        self._title: str | None = None
        self._in_title = False
        self._in_section = False
        self._text: list[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        classes = _classes(attrs)
        if tag == "section" and "currency-section" in classes:
            # These sections are siblings, never nested, so one flag is enough.
            self._in_section = True
            self._title = None
            self._text = []
        elif self._in_section and tag == "h2" and "section-title" in classes:
            self._in_title = True

    def handle_endtag(self, tag: str) -> None:
        if tag == "h2":
            self._in_title = False
        elif tag == "section" and self._in_section:
            if self._title and self._text and self._title not in self.values:
                self.values[self._title] = " ".join(self._text)
            self._in_section = False
            self._title = None
            self._text = []

    def handle_data(self, data: str) -> None:
        text = data.strip()
        if not text or not self._in_section:
            return
        if self._in_title:
            self._title = f"{self._title or ''}{text}".strip()
        elif self._title:
            self._text.append(text)


def _smallest_units(amount: str, decimals: int, *, printed_exactly: bool = False) -> int:
    """Exact conversion of a printed decimal into the asset's smallest unit.

    Refuses anything it cannot convert without rounding. Amounts are printed at
    full precision ("0.001753660000000000"), so a value carrying MORE
    fractional digits than the asset has is a layout we have misread — and
    truncating it would put a wrong number into evidence.

    ``printed_exactly`` demands the stronger property, and only tokens use it.
    A native asset's decimals are a constant in :data:`DEFAULT_SITES`; a
    token's come off a second page, so the printed precision is the only
    independent check we have on them. 3xpl pads every amount to exactly the
    asset's decimals — measured on 6- and 18-decimal contracts, on amounts
    large enough to carry thousands separators, and on **zero**, which prints
    as "0.000000000000000000" rather than "0" and so does not trip this at all.
    A printed precision that does NOT equal the stated decimals therefore means
    the two pages disagree about this contract, and a silent power-of-ten error
    in a figure a court will read is the one outcome worth failing loudly for.
    """
    text = amount.replace(",", "").strip()
    if not _DECIMAL.match(text):
        raise ValueError(f"not a plain decimal amount: {amount!r}")
    whole, _, fraction = text.partition(".")
    if len(fraction) > decimals:
        raise ValueError(f"amount {amount!r} carries more precision than {decimals} decimals")
    if printed_exactly and len(fraction) != decimals:
        raise ValueError(
            f"amount {amount!r} is printed to {len(fraction)} decimal place(s) but the asset "
            f"page states {decimals} — the two pages disagree about this contract's scale"
        )
    return int(whole + fraction.ljust(decimals, "0"))


@dataclass(frozen=True, slots=True)
class _CachedPage:
    """One transaction page, parsed once and reused across capabilities.

    ``EvmAdapter.address_history`` asks the pool for three capabilities, and each
    enumerates from its own module pane, so the three sets of transactions
    overlap rather than coincide: the saving is exactly the transactions that
    appear in more than one pane — a transfer with both an ETH leg and an ERC-20
    leg is listed by both modules, and every one of those was being fetched and
    parsed twice. It is not a flat threefold cut, and claiming one would be
    easy and wrong.

    It is still worth doing here in a way it would not be behind a keyed
    provider. Against a site that permits 25 requests a minute and blocks the
    host for an hour past that, the budget is the scarce thing and every
    duplicate page is spent from it.

    Only TRANSACTION pages are cached, and the distinction is the one the pool's
    own cache policies already draw: a mined transaction is immutable, so a page
    about it can be reused forever. An ADDRESS page is not — new transactions
    land on it — so it is refetched every time and never served from here.

    ``document`` is the manifest entry the answer cites. It is replayed on every
    hit, so a response assembled partly from cache still lists every page its
    rows came from and a reviewer can refetch the lot.
    """

    parser: _TransactionPageParser
    document: Mapping[str, str]


# ── the provider ─────────────────────────────────────────────────────────


class ExplorerFetchProvider(Provider):
    """Last-resort tier: adapter-shaped rows assembled out of public pages.

    Which shape depends on the chain — see :class:`RowEmitter`. Every other
    provider in the pool answers one vendor's dialect; this one answers whatever
    the chain's adapter reads, because it is the only tier serving more than one
    family of them.

    Registered LAST in the pool. Every answer is stamped
    ``explorer-fetch:<host>`` in its provenance — not the bare tier name — so
    a reader can always see which host a conclusion leans on and filter the
    whole tier out of a report if they do not want to rely on it.
    """

    name = "explorer-fetch"

    def __init__(
        self,
        http: httpx.AsyncClient,
        *,
        sites: Mapping[str, ExplorerSite] | None = None,
        user_agent: str = DEFAULT_USER_AGENT,
        rate_per_sec: float = DEFAULT_RATE_PER_SEC,
        site_pages_per_call: int = 10,
        transaction_pages_cached: int = 256,
        robots: RobotsPolicy | None = None,
        clock: Callable[[], float] = time.monotonic,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
    ) -> None:
        if site_pages_per_call < 1:
            raise ValueError("site_pages_per_call must be >= 1")
        self._http = http
        self._sites = dict(sites or DEFAULT_SITES)
        self._headers = {"User-Agent": user_agent}
        self._rate = rate_per_sec
        # One logical page maps onto a FIXED number of explorer pages. It has
        # to be fixed: the caller's cursor is an incrementing integer, so any
        # adaptive stop would leave the next call starting past pages we never
        # read, and that gap would be invisible in the output.
        self._site_pages = site_pages_per_call
        self._robots = robots if robots is not None else RobotsPolicy(http, user_agent=user_agent)
        self._clock = clock
        self._sleep = sleep
        self._buckets: dict[str, TokenBucket] = {}
        # Decimals read off asset pages, kept for the life of the provider. A
        # contract's decimals are immutable, and re-reading the USDT asset page
        # once per transaction would spend the whole crawl budget restating a
        # fact the site already gave us.
        self._decimals: dict[str, int] = {}
        if transaction_pages_cached < 0:
            raise ValueError("transaction_pages_cached must be >= 0")
        self._page_cache_size = transaction_pages_cached
        self._pages: OrderedDict[str, _CachedPage] = OrderedDict()

    @staticmethod
    def _serves(site: ExplorerSite, capability: Capability) -> bool:
        # Both halves are needed to answer at all: `modules` names the URL the
        # rows are enumerated from, `rows.sections` the heading they sit under on
        # the transaction page. A site table listing a module its dialect cannot
        # name a section for would fetch the page and only then KeyError on it —
        # after the request had been spent against a 25-a-minute budget.
        return capability in site.modules and capability in site.rows.sections

    def supports(self, chain: str, capability: Capability) -> bool:
        site = self._sites.get(chain)
        return site is not None and self._serves(site, capability)

    async def execute(self, request: ProviderRequest) -> ProviderResponse:
        site = self._sites.get(request.chain)
        if site is None or not self._serves(site, request.capability):
            raise ProviderResponseInvalid(
                self.name, f"no site serves {request.capability} on {request.chain}"
            )
        provider = f"{self.name}:{site.host}"
        mark = request.params.get("fingerprint")
        if mark and not site.rows.caller_pages_by_number:
            return self._declined_continuation(site, provider, request, str(mark))
        address = site.rows.canonical(str(request.params["address"]).strip())
        page = int(request.params.get("page", 1))
        if page < 1:
            raise ProviderResponseInvalid(provider, f"page must be >= 1, got {page}")

        read: list[dict[str, str]] = []
        rows: list[dict[str, Any]] = []
        transactions, truncated = await self._enumerate(
            site, address, request.capability, page, read
        )
        for tx_hash in transactions:
            rows.extend(await self._rows_for(site, tx_hash, request.capability, address, read))

        # The digest a finding cites has to address something reproducible, and
        # this answer is assembled from many pages rather than one vendor body.
        # So `raw` is a manifest of every page read with its own digest: a
        # reviewer can refetch those URLs and check the derivation instead of
        # taking the assembled rows on trust.
        manifest: dict[str, Any] = {
            "tier": self.name,
            "provider": provider,
            "chain": site.chain,
            "capability": str(request.capability),
            "address": address,
            "page": page,
            "documents": read,
            "rows": rows,
        }
        if truncated:
            # The site still had pages and this caller has no way to ask for
            # them (see `_enumerate`). The rows are real; the READ is short, and
            # a short read that says nothing is indistinguishable from an
            # address whose history ends here. It goes in the manifest because
            # the manifest is what a reviewer checks the derivation against —
            # and, since the manifest is read by no adapter, in the ENVELOPE
            # too (`SHORT_READ_KEY`), which is the copy the engine sees.
            manifest["truncated_at_page_limit"] = self._site_pages
        raw = canonical_json_bytes(manifest)
        return ProviderResponse(
            provider=provider,
            retrieved_at=datetime.now(UTC),
            # The rows go out in the envelope this chain's adapter unwraps; the
            # manifest above stays flat, because what a reviewer refetches is
            # pages, not a vendor's pagination.
            payload=site.rows.envelope(rows, truncated=truncated),
            raw=raw,
            payload_sha256=sha256_hex(raw),
        )

    def _declined_continuation(
        self, site: ExplorerSite, provider: str, request: ProviderRequest, mark: str
    ) -> ProviderResponse:
        """A cursor this tier cannot position, answered as the short read it is.

        Two things this deliberately does NOT do, and both are the point.

        **It does not serve page one.** The caller already holds those rows and
        is asking what comes after them; answering from the top would hand them
        back and then report the feed finished — a hole in the middle of a
        history, wearing the shape of a completed read.

        **It does not raise.** The obvious decline is ``ProviderResponseInvalid``
        and that is what this used to be, but ``ProviderPool`` charges that class
        to ``reg.breaker.record_failure()``. The breaker belongs to a REGISTERED
        PROVIDER, not to a chain, and ``runtime.py`` registers this tier once for
        every chain it serves — so five declined Tron continuations opened the
        circuit and took ETHEREUM's floor tier down with them, for the length of
        the reset timeout, over a question Ethereum never asked. A decline this
        tier's own contract promises is not evidence that the tier is unhealthy,
        and charging it as one disables a working fallback for a working chain.

        What goes back instead is the true answer to what was asked: no rows,
        and :data:`SHORT_READ_KEY` saying that the emptiness is a read cut short
        rather than the end of the history. ``TronAdapter`` merges that into an
        empty page with ``truncated=True`` and the engine marks the address
        read-in-part — the same coverage fact the pages-per-call stop records,
        reached from the other side. Failing instead cost strictly more than the
        continuation: the adapter awaits its native feed hard, so the pool's
        ``AllProvidersFailed`` took the whole address down with the one page
        nobody could position.

        The manifest names the cursor and carries no documents, because none
        were fetched — not even ``robots.txt``. A reviewer refetching this
        derivation must find nothing to refetch, and must be told why.

        Two consequences of answering rather than raising, both weighed:

        - **The pool caches it**, where it cached nothing before, under a key
          that includes this cursor. It stays harmless only because the page it
          produces carries no ``next_cursor``: pagination ends there, so nothing
          asks for this exact cursor again, and a fresh read of the same address
          starts at no fingerprint at all — a different key, served by whichever
          provider is healthy. A future caller that RETRIES a cursor would want
          this reconsidered.
        - **Nothing below this tier is asked.** A failure fails over; an answer
          does not. This tier is registered last (``runtime.build_provider_pool``,
          priority 95) precisely because it is the floor, so today there is
          nothing below it to ask — but registering a provider under it that CAN
          position another vendor's cursor would need this branch to move.
        """
        logger.info(
            "%s: declining to continue %s for %s from cursor %r — %s pages by opaque cursor and "
            "this tier reads numbered pages; answering the short read instead so the address is "
            "recorded as read in part rather than as finished",
            site.host,
            site.modules[request.capability],
            request.params.get("address"),
            mark[:32],
            site.chain,
        )
        manifest: dict[str, Any] = {
            "tier": self.name,
            "provider": provider,
            "chain": site.chain,
            "capability": str(request.capability),
            "address": site.rows.canonical(str(request.params.get("address", "")).strip()),
            "declined_continuation": mark,
            "documents": [],
            "rows": [],
        }
        raw = canonical_json_bytes(manifest)
        return ProviderResponse(
            provider=provider,
            retrieved_at=datetime.now(UTC),
            payload=site.rows.envelope([], truncated=True),
            raw=raw,
            payload_sha256=sha256_hex(raw),
        )

    async def _enumerate(
        self,
        site: ExplorerSite,
        address: str,
        capability: Capability,
        page: int,
        read: list[dict[str, str]],
    ) -> tuple[list[str], bool]:
        """Transaction ids for one logical page, read off consecutive site pages.

        The flag is True when the site still had pages and the caller has no way
        to ask for them — see :attr:`RowEmitter.caller_pages_by_number`.
        """
        provider = f"{self.name}:{site.host}"
        collected: list[str] = []
        page_size: int | None = None
        exhausted = False
        for offset in range(self._site_pages):
            path = site.address_path(address, capability, (page - 1) * self._site_pages + offset)
            parser = _AddressPageParser(site.rows)
            parser.feed((await self._get(site, path, read)).decode("utf-8", errors="replace"))
            pane = parser.active
            if pane is None:
                raise ProviderResponseInvalid(
                    provider,
                    f"{path}: no events table found — the page names no active module pane "
                    f"({len(parser.panes)} pane(s), {parser.tabs} tab(s), "
                    f"active {parser.active_index}). A pane/tab count that disagrees means the "
                    "tab strip no longer indexes the panes, so which module is on screen cannot "
                    "be known — refusing to read a pane that may belong to another module",
                )
            if pane.transactions and not pane.names_transactions:
                raise ProviderResponseInvalid(
                    provider,
                    f"{path}: the module's pane lists rows under no transaction column "
                    f"(header cells: {pane.header_cells[:6]})",
                )
            if not pane.transactions and not pane.proves_empty:
                raise ProviderResponseInvalid(
                    provider,
                    f"{path}: the module's pane holds neither rows, an empty marker, nor a "
                    "'no events' notice — refusing to report this address as having no history",
                )
            collected.extend(tx for tx in pane.transactions if tx not in collected)
            # Page size is measured in ROWS, not in distinct ids. One transaction
            # can occupy two rows (an address that paid itself appears as both
            # endpoints), and counting ids would read that full page as short and
            # stop — dropping every page after it with nothing recording the loss.
            count = pane.rows
            if count == 0 or (page_size is not None and count < page_size):
                exhausted = True
                break  # a short page is the last page
            if page_size is None:
                page_size = count  # the site's page size, learned from its first page
        # Stopping on the bound rather than on a short page is normal where the
        # caller pages by number — it asks for the next one and this tier reads
        # on. Where it does not, this is where the history quietly ends: the rows
        # past the bound are never fetched and the answer carries no cursor to
        # say so. It is still the right trade against minting a cursor nobody can
        # honour, but it is not allowed to be invisible.
        truncated = not exhausted and not site.rows.caller_pages_by_number
        if truncated:
            logger.warning(
                "%s: read %d page(s) of %s for %s and the site still had more; %s pages by "
                "opaque cursor, which this tier cannot mint, so the rest of this address's "
                "history is not in this answer",
                site.host,
                self._site_pages,
                site.modules[capability],
                address,
                site.chain,
            )
        return collected, truncated

    async def _rows_for(
        self,
        site: ExplorerSite,
        tx_hash: str,
        capability: Capability,
        address: str,
        read: list[dict[str, str]],
    ) -> list[dict[str, Any]]:
        provider = f"{self.name}:{site.host}"
        path = site.transaction_path(tx_hash)
        parser = await self._transaction_page(site, path, read)
        if parser.partial:
            raise ProviderResponseInvalid(
                provider, f"{path}: {parser.partial} transfer block(s) could not be read"
            )
        wanted = site.rows.sections[capability]
        transfers = parser.sections.get(wanted)
        if transfers is None:
            # 3xpl does not print an empty section: a transaction with none of
            # a kind collapses it into the "Additional events" notice, and that
            # notice is the site SAYING none — but only about the modules it
            # lists. A notice about ERC-721 proves nothing about ERC-20, and
            # accepting it would empty a whole feed the day a heading is
            # renamed, with no error anywhere to show for it.
            module = site.rows.empty_types[capability]
            if not _notice_names(parser.empty_notice, module):
                raise ProviderResponseInvalid(
                    provider,
                    f"{path}: no {wanted!r} section, and no {_EMPTY_SECTION!r} notice naming "
                    f"{module!r} as absent (notice: {parser.empty_notice!r})",
                )
            return []
        if not transfers:
            # The heading is there and not one transfer under it parsed. On
            # every recorded page a section that exists has content, so this is
            # the transfer markup having moved — and answering "this
            # transaction moved nothing" would drain the value out of a whole
            # address history without a single error to show for it.
            raise ProviderResponseInvalid(
                provider, f"{path}: a {wanted!r} section with no readable transfer under it"
            )
        if parser.block_number is None or parser.timestamp_ms is None:
            raise ProviderResponseInvalid(
                provider, f"{path}: transfers present but no block height or timestamp"
            )
        if capability is Capability.ADDRESS_HISTORY and len(transfers) > 1:
            # One top-level row per transaction, on both dialects — `txlist` is
            # a row per hash and TronGrid's native feed is a row per `txID`, and
            # both callers key on that id and keep the last row they see.
            # Emitting several would drop real value with nothing registering
            # that it happened.
            raise ProviderResponseInvalid(
                provider, f"{path}: {len(transfers)} top-level transfers, expected at most one"
            )
        stamp = _Stamp(
            tx_hash=tx_hash,
            block_number=parser.block_number,
            timestamp_ms=parser.timestamp_ms,
        )
        if capability is Capability.TOKEN_TRANSFERS:
            return await self._token_rows(site, path, address, transfers, stamp, read)
        rows: list[dict[str, Any]] = []
        for transfer in transfers:
            if transfer.asset_id != site.native_asset_id:
                continue  # native value only; the token leg is its own capability
            try:
                amount = _smallest_units(transfer.amount, site.native_decimals)
            except ValueError as exc:
                raise ProviderResponseInvalid(provider, f"{path}: {exc}") from exc
            rows.append(site.rows.native_row(stamp, transfer, amount=amount))
        if not rows:
            # The section exists and is populated, and not one transfer in it
            # was for this chain's native asset. The address's own pane named
            # this transaction, so the two pages contradict each other — the
            # same fault `_token_rows` raises on, and it is left symmetrical
            # deliberately.
            #
            # Silence here is the expensive option. A dropped transaction is
            # invisible: the pane promised a row and the feed simply returns
            # one fewer. Where every transaction on an address does it, the
            # feed empties completely — and for ADDRESS_HISTORY that is the one
            # failure with no recovery, because it is the feed that must not
            # degrade: zero native rows reaches the engine as "this address
            # never transacted", which nothing downstream can tell from the
            # truth. Measured: a populated `Main events` section holding one
            # token transfer returned zero rows and no error.
            raise ProviderResponseInvalid(
                provider,
                f"{path}: {len(transfers)} transfer(s) in the {wanted!r} section, none of them "
                f"in {site.native_asset_id!r} — the pane listed this transaction, so the two "
                "pages disagree about what it moved",
            )
        return rows

    async def _transaction_page(
        self, site: ExplorerSite, path: str, read: list[dict[str, str]]
    ) -> _TransactionPageParser:
        """One parsed transaction page, fetched at most once per provider.

        The three capabilities of a single ``address_history`` call each resolve
        the same set of transaction pages; see :class:`_CachedPage` for why
        paying for them once matters so much more here than it would behind a
        keyed provider.
        """
        url = f"{site.base_url.rstrip('/')}{path}"
        hit = self._pages.get(url)
        if hit is not None:
            self._pages.move_to_end(url)
            # The manifest still names the page, so an answer served from cache
            # cites exactly what an answer served from the network would.
            read.append(dict(hit.document))
            return hit.parser
        before = len(read)
        body = await self._get(site, path, read)
        parser = _TransactionPageParser(site.rows)
        parser.feed(body.decode("utf-8", errors="replace"))
        if self._page_cache_size:
            # `_get` appends exactly one manifest entry on success, so this is
            # THIS page's citation. Reused rather than recomputed, so an answer
            # served from cache cites byte-identically to the first one.
            self._pages[url] = _CachedPage(parser=parser, document=read[before])
            while len(self._pages) > self._page_cache_size:
                self._pages.popitem(last=False)
        return parser

    async def _token_rows(
        self,
        site: ExplorerSite,
        path: str,
        address: str,
        transfers: Sequence[_Transfer],
        stamp: _Stamp,
        read: list[dict[str, str]],
    ) -> list[dict[str, Any]]:
        """Token rows: the transfers in this transaction touching ``address``.

        A transaction page lists every token transfer in the transaction, not
        the ones this address took part in — the airdrop that reached the case
        study address carries 500 of them between 501 addresses. Emitting all
        of them would put 500 strangers into the frontier off one piece of
        spam, and would not be the feed `tokentx?address=` describes either.
        """
        provider = f"{self.name}:{site.host}"
        module = site.modules[Capability.TOKEN_TRANSFERS]
        section = site.rows.sections[Capability.TOKEN_TRANSFERS]
        mine = [t for t in transfers if address in (t.sender, t.recipient)]
        if not mine:
            # The address's own token pane is what named this transaction, so
            # a section where it appears nowhere is the two pages contradicting
            # each other. Dropping the transaction here would leave a hole in a
            # feed that still looked complete, which is the failure this tier is
            # least allowed to have.
            raise ProviderResponseInvalid(
                provider,
                f"{path}: {len(transfers)} token transfer(s), none with {address} at either end, "
                f"yet its own {module} pane listed this transaction",
            )
        rows: list[dict[str, Any]] = []
        for transfer in mine:
            prefix, _, contract = transfer.asset_id.partition("/")
            if prefix != module or not site.rows.token_contract.fullmatch(contract):
                raise ProviderResponseInvalid(
                    provider,
                    f"{path}: {transfer.asset_id!r} in the {section} section is not a "
                    f"{module} contract",
                )
            decimals = await self._token_decimals(site, transfer.asset_id, read)
            try:
                amount = _smallest_units(transfer.amount, decimals, printed_exactly=True)
            except ValueError as exc:
                raise ProviderResponseInvalid(provider, f"{path}: {exc}") from exc
            rows.append(
                site.rows.token_row(
                    stamp, transfer, contract=contract, decimals=decimals, amount=amount
                )
            )
        return rows

    async def _token_decimals(
        self, site: ExplorerSite, asset_id: str, read: list[dict[str, str]]
    ) -> int:
        """The contract's scale, as the asset page states it. Never inferred.

        The whole token capability rests on this page. Getting a number out of
        it by any other route — counting the digits a transaction happened to
        print, defaulting to 18 — would put a scale into ``assets``, which keys
        on ``(chain, kind, contract)`` with ``decimals`` outside the key, and
        silently redefine that contract for every row a keyed provider wrote.
        So every failure here is an error, and none of them is a default.
        """
        cached = self._decimals.get(asset_id)
        if cached is not None:
            return cached
        provider = f"{self.name}:{site.host}"
        path = site.asset_path(asset_id)
        parser = _AssetPageParser()
        parser.feed((await self._get(site, path, read)).decode("utf-8", errors="replace"))
        stated = parser.values.get(_ASSET_ID)
        if stated != asset_id:
            # A redirect, or the page for some other contract. Its decimals
            # would be a fact about the wrong token.
            raise ProviderResponseInvalid(
                provider, f"{path}: the asset page identifies itself as {stated!r}"
            )
        digits = parser.values.get(_ASSET_DECIMALS, "")
        if not digits.isdigit():
            raise ProviderResponseInvalid(
                provider, f"{path}: no {_ASSET_DECIMALS!r} stated (found {digits!r})"
            )
        decimals = int(digits)
        self._decimals[asset_id] = decimals
        return decimals

    async def _get(self, site: ExplorerSite, path: str, read: list[dict[str, str]]) -> bytes:
        """One page, after robots and the rate limit.

        Appends EXACTLY ONE entry to ``read`` on success and none on any
        failure. ``_transaction_page`` relies on that to pick this page's
        citation back out for the cache, so it is a contract rather than an
        incidental behaviour.
        """
        provider = f"{self.name}:{site.host}"
        decision = await self._robots.decide(site.base_url, path)
        if not decision.allowed:
            # Declining must not look like an outage: ProviderResponseInvalid
            # is never retried, so the pool moves on instead of knocking again
            # at a door that already said no.
            raise ProviderResponseInvalid(provider, f"declined: {decision.reason}")
        await self._bucket(site, decision.crawl_delay).acquire()
        url = f"{site.base_url.rstrip('/')}{path}"
        # 429 never reaches the check below: `perform` maps it to
        # ProviderRateLimited, so the pool penalizes the bucket and retries THIS
        # provider rather than discarding the tier. That is the correct handling
        # and it is why the escalation to 403 is the dangerous one — see
        # DEFAULT_RATE_PER_SEC for the measurement.
        response = await perform(self._http, provider, "GET", url, headers=self._headers)
        if response.status_code == 403:
            # The site has stopped serving this host entirely, for an hour at a
            # time. Reported in those words because "HTTP 403 for /ethereum/…"
            # reads like one bad path, and an operator who believes that will go
            # looking at the parser while every capability on every chain is
            # dark. Still ProviderResponseInvalid: never retried, immediate
            # failover, and the run records the feed as unavailable — which is
            # the honest outcome, since there is no polite way past a refusal.
            retry_after = response.headers.get("Retry-After", "unknown")
            raise ProviderResponseInvalid(
                provider,
                f"{site.host} is refusing this host (HTTP 403, Retry-After {retry_after}s) — "
                "the crawl rate exceeded what the site allows and it has stopped answering; "
                "no page on this site can be read until that expires",
            )
        if response.status_code >= 400:
            # 4xx is the site refusing or the page being gone. Neither is
            # transient and neither is ours to retry past.
            raise ProviderResponseInvalid(provider, f"HTTP {response.status_code} for {path}")
        if response.status_code >= 300:
            # Redirects are not followed, so a 3xx body is not the page. It would
            # fail the pane check a moment later anyway — no false empty — but as
            # "no active module pane", which reads as the parser breaking rather
            # than as a URL layout that moved. Naming it is the difference
            # between a five-minute fix and an afternoon in the parser.
            raise ProviderResponseInvalid(
                provider,
                f"{path} redirected (HTTP {response.status_code} to "
                f"{response.headers.get('Location', 'an unstated location')!r}) — this tier does "
                "not follow redirects, so the page layout this module encodes may have moved",
            )
        remaining = response.headers.get(_RATELIMIT_REMAINING)
        if remaining is not None and remaining.isdigit() and int(remaining) <= 2:
            # The last warning before the interstitial. Logged rather than acted
            # on: this tier paces itself from a rate it can state and defend, and
            # silently re-steering from a vendor header would hide the day that
            # header starts meaning something else.
            logger.warning(
                "%s reports %s request(s) left in its rate budget; the configured rate of "
                "%.3f/s may be too fast for this site",
                site.host,
                remaining,
                self._rate,
            )
        read.append({"url": url, "sha256": sha256_hex(response.content)})
        return response.content

    def _bucket(self, site: ExplorerSite, crawl_delay: float | None) -> TokenBucket:
        bucket = self._buckets.get(site.host)
        if bucket is None:
            # Crawl-delay may only slow us down. A site asking for one request
            # every 10s gets one every 10s; a site asking for 0.1s still gets
            # our own conservative rate.
            rate = self._rate
            if crawl_delay is not None and crawl_delay > 0:
                rate = min(rate, 1.0 / crawl_delay)
            bucket = TokenBucket(rate, 1.0, clock=self._clock, sleep=self._sleep)
            self._buckets[site.host] = bucket
        return bucket


__all__ = [
    "DEFAULT_RATE_PER_SEC",
    "DEFAULT_SITES",
    "DEFAULT_USER_AGENT",
    "ETHERSCAN_ROWS",
    "TRONGRID_ROWS",
    "ExplorerFetchProvider",
    "ExplorerSite",
    "RobotsDecision",
    "RobotsPolicy",
    "RowEmitter",
]
