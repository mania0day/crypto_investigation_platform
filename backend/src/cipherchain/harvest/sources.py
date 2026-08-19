"""What a harvest source is, and why it is split in two halves.

A source is a **document plus its provenance** and a **parser**. Loading and
parsing are separate methods on purpose: the parser is then a pure function of
bytes, so every source ships with a recorded fixture and its test touches no
network at all. That is not a stylistic preference here — it is the only way
this code could be tested on the machine it was written on, where the exchange
sites it exists to read are unreachable (see :mod:`cipherchain.harvest.exchanges`).

Two rules are enforced at construction rather than at ingest, because a bad
source should fail before it can write anything:

- **A harvest source declares a trusted method.** ``scripts/import_labelpacks``
  already refuses a pack whose method is not one of the vetted tiers, for the
  reason that an unvetted method arrives ``pending`` and 75,000 labels able to
  name nothing is a failure that looks like success. Same rule, same refusal.
- **A source never invents its date.** A claim a reader cannot date is a claim
  a reader cannot weigh. Formats that carry their own date (labelpacks, the
  ``lastUpdatedAt`` on Coinbase's reserves page) use it; formats that do not (a
  proof-of-reserves CSV) take it from the drop's file name, which is the
  operator's own declaration and is visible on disk.

A third rule arrived later, from the bug this module shipped with: **a source
declares how long its silence may last** (:attr:`SourceSpec.stale_after_days`).
The original design could only report a source that failed TODAY, and the
failure this subsystem actually has is a source that succeeds every day on a
document nobody has republished since June — the drop file stays on disk, every
claim re-ingests as ``unchanged``, and the cycle exits 0 while coverage rots.
Cadence differs per publisher (Coinbase's reserves page restates itself hourly,
an exchange's proof-of-reserves is monthly), so the threshold belongs on the
source rather than on the scheduler.
"""

from __future__ import annotations

import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Protocol
from urllib.parse import urljoin, urlsplit

import httpx

from cipherchain.core.errors import CipherChainError
from cipherchain.intel.policy import TRUSTED_METHODS, IntelClaim
from cipherchain.investigation.attribution import CATEGORY_VASP, AddressRole
from cipherchain.providers.clients.explorer_fetch import RobotsPolicy

# A publisher that restates its disclosure monthly, plus slack for a late
# month. Deliberately generous: the alarm exists to catch a source that has
# stopped, and one that cries every fourth week gets muted, which returns the
# subsystem to having no alarm at all.
DEFAULT_STALE_AFTER_DAYS = 35

# Says who this is and what it is doing, so a site operator reading their logs
# can block it deliberately. Same posture as the fetch tier's
# (``providers/clients/explorer_fetch.py``) — attribution instead of
# impersonation — but a distinct string, because this is a different job on a
# different schedule and an operator should be able to tell them apart.
DEFAULT_USER_AGENT = (
    "CipherChain-harvester/0.1 (+blockchain investigation research; public pages only)"
)


class HarvestError(CipherChainError):
    """A harvest source could not contribute this cycle."""


class SourceUnavailable(HarvestError):
    """Nothing to read — no drop, or the fetch failed.

    Not a defect. A cycle where one source has published nothing new is a
    normal cycle, and it must not abort the sources that follow it.
    """


class SourceRejected(HarvestError):
    """The document is not what the source declared it to be.

    Separate from :class:`SourceUnavailable` because the operator response
    differs: unavailable means wait, rejected means look at the file.
    """


@dataclass(frozen=True, slots=True)
class SourceSpec:
    """Everything a source asserts about itself, before any bytes are read."""

    name: str  # recorded as `source` on every claim; a source cannot corroborate itself
    entity: str  # the operator being named
    method: str
    document_url: str  # where the disclosure is published, for the evidence trail
    category: str = CATEGORY_VASP
    role: str = str(AddressRole.OPERATIONAL)
    confidence: float = 0.8
    # How long this publisher may go without restating its disclosure before a
    # cycle should shout. Not "how often we poll": the harvester polls daily
    # regardless, and this is the age of the DOCUMENT it keeps getting back.
    stale_after_days: int = DEFAULT_STALE_AFTER_DAYS

    def __post_init__(self) -> None:
        if self.method not in TRUSTED_METHODS:
            raise SourceRejected(
                f"{self.name}: method {self.method!r} is not a trusted harvest tier "
                f"({', '.join(sorted(TRUSTED_METHODS))}) — every claim it produced would "
                "arrive pending and name nothing"
            )
        if not (0.0 < self.confidence < 1.0):
            raise SourceRejected(
                f"{self.name}: confidence must be in (0, 1) — a claim is never proof"
            )
        if self.stale_after_days <= 0:
            # Zero would mark the source stale the moment it succeeded, and an
            # alarm that is always on is an alarm nobody reads.
            raise SourceRejected(
                f"{self.name}: stale_after_days must be at least 1 day, got {self.stale_after_days}"
            )


@dataclass(frozen=True, slots=True)
class HarvestDocument:
    """One retrieved document and where it came from."""

    raw: bytes
    origin: str  # the published url — travels onto the claim as evidence_url
    declared_date: datetime | None
    # Which parser read it. Carried on the document rather than re-derived at
    # parse time so the bytes and the reading of them cannot disagree.
    media: str | None = None


class DocumentParser(Protocol):
    """Bytes in, claims out. Pure, so a fixture is a complete test."""

    def __call__(
        self, document: HarvestDocument, *, spec: SourceSpec, retrieved_at: datetime
    ) -> list[IntelClaim]: ...


class DocumentLoader(Protocol):
    """Just the retrieval half. Split out so one source can have two ways in —
    fetch it, or read what an operator dropped — without either of them
    needing to know the other exists."""

    async def load(self) -> HarvestDocument: ...


class HarvestSource(Protocol):
    """One pluggable unit: how to get the document, how to read it."""

    spec: SourceSpec

    async def load(self) -> HarvestDocument: ...

    def parse(self, document: HarvestDocument, *, retrieved_at: datetime) -> list[IntelClaim]: ...


def parse_by_media(
    document: HarvestDocument,
    *,
    spec: SourceSpec,
    parsers: Mapping[str, DocumentParser],
    retrieved_at: datetime,
) -> list[IntelClaim]:
    """Pick the parser the DOCUMENT says it needs, never the one the source
    expected. ``media`` is set by whoever retrieved the bytes, so a source with
    both a fetch path and a drop path cannot read a saved HTML page as a
    labelpack because that is what it usually gets."""
    parser = parsers.get(document.media or "")
    if parser is None:
        raise SourceRejected(
            f"{spec.name}: no parser for media {document.media!r} "
            f"(have {', '.join(sorted(parsers)) or 'none'})"
        )
    return parser(document, spec=spec, retrieved_at=retrieved_at)


# `<source-name>__<YYYY-MM-DD>.<ext>`. The date is part of the file name rather
# than the file's mtime because mtime records when the file was COPIED, and a
# disclosure's publication date is the thing a reader needs to weigh it.
_DROP_NAME = re.compile(r"^(?P<name>.+?)__(?P<date>\d{4}-\d{2}-\d{2})\.(?P<ext>[A-Za-z0-9]+)$")


class ManualDropSource:
    """A source an operator feeds by hand, from a directory the worker watches.

    This exists because "fetch it yourself" is not always available: the host
    this was built on cannot reach binance.com or okx.com at all (verified
    three times — see :mod:`cipherchain.harvest.exchanges`). The drop path keeps the
    lifecycle identical: the file still declares its provenance, the claims
    still go through the intel service, and nothing writes to the label store
    directly.

    Newest declared date wins, and older drops are left in place — deleting an
    operator's file is not this worker's business, and the record of what was
    ingested when lives in ``label_events`` regardless.
    """

    def __init__(
        self,
        spec: SourceSpec,
        drop_dir: Path,
        *,
        parsers: Mapping[str, DocumentParser],
    ) -> None:
        self.spec = spec
        self._dir = drop_dir
        self._parsers = dict(parsers)

    def _newest(self) -> tuple[Path, datetime, str]:
        candidates: list[tuple[datetime, Path, str]] = []
        for path in sorted(self._dir.glob(f"{self.spec.name}__*")) if self._dir.is_dir() else []:
            match = _DROP_NAME.match(path.name)
            if match is None or match.group("name") != self.spec.name:
                continue
            extension = match.group("ext").lower()
            if extension not in self._parsers:
                continue
            # The pattern admits `2026-13-45`: it matches digit shapes, not real
            # days. A date nobody can read is the operator's to correct, and
            # saying so beats letting a ValueError out of a source that is
            # supposed to be able to fail on its own.
            try:
                declared_date = datetime.fromisoformat(match.group("date")).replace(tzinfo=UTC)
            except ValueError as exc:
                raise SourceRejected(
                    f"{self.spec.name}: {path.name} is dated {match.group('date')!r}, "
                    "which is not a date"
                ) from exc
            candidates.append((declared_date, path, extension))
        if not candidates:
            raise SourceUnavailable(
                f"{self.spec.name}: no drop in {self._dir} matching "
                f"'{self.spec.name}__<YYYY-MM-DD>.<{'|'.join(sorted(self._parsers))}>'"
            )
        declared, path, extension = max(candidates, key=lambda item: (item[0], item[1].name))
        return path, declared, extension

    async def load(self) -> HarvestDocument:
        path, declared, extension = self._newest()
        try:
            raw = path.read_bytes()
        except OSError as exc:
            raise SourceUnavailable(f"{self.spec.name}: cannot read {path}: {exc}") from exc
        # `origin` stays the PUBLISHED url, not the local path: the citation an
        # investigator needs is where the disclosure lives, not where a copy of
        # it happened to sit on one operator's disk.
        return HarvestDocument(
            raw=raw,
            origin=self.spec.document_url,
            declared_date=declared,
            media=extension,
        )

    def parse(self, document: HarvestDocument, *, retrieved_at: datetime) -> list[IntelClaim]:
        return parse_by_media(
            document, spec=self.spec, parsers=self._parsers, retrieved_at=retrieved_at
        )


class HttpDocumentSource:
    """A source the worker fetches itself, on a host that can reach it.

    The primary path wherever the network allows it; the drop above is the
    fallback, not the other way round. Any transport failure or non-2xx answer
    becomes :class:`SourceUnavailable` so one unreachable publisher slows the
    cycle down instead of ending it.

    Three boundaries, all of them the fetch tier's
    (``providers/clients/explorer_fetch.py``) and none of them negotiable here
    either:

    - ``robots.txt`` is read and obeyed BEFORE the document is requested, using
      that module's :class:`RobotsPolicy` rather than a second opinion written
      here. It fails CLOSED, and a disallow is a decline — which in this
      package has a useful consequence: the source falls through to whatever
      drop path sits behind it, so a publisher that asks robots not to be
      crawled gets fetched by a human instead of by us.
    - **Redirects are not followed**, except onto a host the source named in
      advance. A 3xx is otherwise reported, loudly, as the configured document
      url having moved. This is the guard that was missing: the Coinbase url
      shipped in this file 404'd for an unknown length of time while the cycle
      reported nothing worse than "unavailable", and a source whose address has
      changed needs a person to go and re-verify what the new page actually
      publishes — not an automatic follow onto a document nobody has read.
      ``follow_redirect_hosts`` is that person's decision, written down: OFAC's
      download endpoint answers 302 onto a signed, one-hour storage url
      (:mod:`cipherchain.harvest.sanctions`), which is a handoff to the same
      publisher's own bucket rather than a relocation. Naming the host in code
      keeps the review where it belongs — at authoring time, once — and any
      OTHER destination still stops the source dead. The host is half of it:
      the hop must also be **https**, because what these documents become is
      `active` first-party labels that need nothing to corroborate them, and a
      list fetched in cleartext is one an on-path attacker can rewrite.
    - One request per source per cycle — two when a handoff is followed, and
      the second is to a different host — so a ``Crawl-delay`` has nothing to
      pace; if a second source is ever pointed at the same host, that stops
      being true and this class needs the fetch tier's token bucket too.

    ``timeout`` overrides the client's for the DOCUMENT request only. A client
    shared by every source cannot be sized for the largest of them, and the
    alternative — raising it globally — would hand a stalled small page the
    same patience a 28 MB download needs.
    """

    def __init__(
        self,
        spec: SourceSpec,
        http: httpx.AsyncClient,
        *,
        parser: DocumentParser,
        url: str | None = None,
        media: str | None = None,
        user_agent: str = DEFAULT_USER_AGENT,
        robots: RobotsPolicy | None = None,
        timeout: float | None = None,
        follow_redirect_hosts: frozenset[str] = frozenset(),
    ) -> None:
        self.spec = spec
        self._http = http
        self._parser = parser
        self._url = url or spec.document_url
        self._media = media
        self._headers = {"User-Agent": user_agent}
        self._robots = robots if robots is not None else RobotsPolicy(http, user_agent=user_agent)
        self._timeout = timeout
        self._follow_redirect_hosts = follow_redirect_hosts

    async def load(self) -> HarvestDocument:
        split = urlsplit(self._url)
        decision = await self._robots.decide(f"{split.scheme}://{split.netloc}", split.path or "/")
        if not decision.allowed:
            raise SourceUnavailable(f"{self.spec.name}: {decision.reason}")
        response = await self._get(self._url)
        if response.is_redirect:
            response = await self._handoff(response)
        if response.status_code != 200:
            raise SourceUnavailable(
                f"{self.spec.name}: {self._url} answered HTTP {response.status_code}"
            )
        # `origin` is the CONFIGURED url even after a handoff. The url a signed
        # redirect leads to expires within the hour, so recording it as the
        # evidence trail would cite a document no reader could ever open.
        return HarvestDocument(
            raw=response.content, origin=self._url, declared_date=None, media=self._media
        )

    async def _get(self, url: str) -> httpx.Response:
        # Two call sites rather than one with `timeout=self._timeout`: httpx
        # reads `timeout=None` as "wait forever", not as "use the client's", so
        # passing the unset value through would silently remove the ceiling
        # from every source that never asked for one.
        try:
            if self._timeout is None:
                return await self._http.get(url, headers=self._headers)
            return await self._http.get(url, headers=self._headers, timeout=self._timeout)
        except httpx.HTTPError as exc:
            raise SourceUnavailable(f"{self.spec.name}: {url} unreachable ({exc!r})") from exc

    async def _handoff(self, response: httpx.Response) -> httpx.Response:
        """Follow a 3xx exactly one hop, and only onto a pre-declared host over TLS."""
        location = response.headers.get("location", "")
        target = urljoin(self._url, location)
        split = urlsplit(target)
        host = split.netloc
        if host not in self._follow_redirect_hosts:
            raise SourceUnavailable(
                f"{self.spec.name}: {self._url} answered HTTP {response.status_code} to "
                f"{location or '?'!r} — the configured document url has moved and the new "
                "one has not been read by anybody"
            )
        if split.scheme != "https":
            # The host being pre-declared says WHO may serve the handoff, not
            # that anybody may serve it in the clear. What comes back here is a
            # whole sanctions list that lands as `active` first-party labels
            # needing no corroboration, so an on-path attacker who rewrote it
            # in flight could un-sanction any address in it and the cycle would
            # report an ordinary morning. The declared host was reviewed for a
            # signed HTTPS handoff; this is a different thing wearing its name.
            raise SourceUnavailable(
                f"{self.spec.name}: {self._url} answered HTTP {response.status_code} to "
                f"{location or '?'!r} — {host} is a declared handoff host, but this would "
                "fetch the document in cleartext, and every claim in it arrives active"
            )
        followed = await self._get(target)
        if followed.is_redirect:
            # One hop is a handoff; a chain is a redirect somebody should look
            # at. Refusing here also means this cannot loop.
            raise SourceUnavailable(
                f"{self.spec.name}: {host} redirected again — a handoff is one hop, and a "
                "chain of them is not a document anybody has read"
            )
        return followed

    def parse(self, document: HarvestDocument, *, retrieved_at: datetime) -> list[IntelClaim]:
        return self._parser(document, spec=self.spec, retrieved_at=retrieved_at)


class FirstAvailableSource:
    """One source, several ways in — the first that yields bytes wins.

    Coinbase is why this exists. Its reserves page IS fetchable from a host
    with a route to it, and is not from a host without one; making those two
    situations two different *sources* would split one publisher's claims
    across two identities in a store keyed on ``(chain, address, source)`` —
    and ``corroborates()`` only asks for a DIFFERENT source, so the fetched
    copy and the hand-saved copy of the same page would end up corroborating
    each other. One spec, one identity, two transports.

    Parsing dispatches on the document's own ``media`` (:func:`parse_by_media`)
    rather than on which loader answered, so the reading of the bytes follows
    the bytes.

    Every loader's reason is kept and reported together. "Coinbase contributed
    nothing" is not actionable; "robots declined and there is no drop either"
    tells the operator exactly which of the two to go and do.
    """

    def __init__(
        self,
        spec: SourceSpec,
        loaders: Sequence[DocumentLoader],
        *,
        parsers: Mapping[str, DocumentParser],
    ) -> None:
        self.spec = spec
        self._loaders = list(loaders)
        self._parsers = dict(parsers)

    async def load(self) -> HarvestDocument:
        reasons: list[str] = []
        for loader in self._loaders:
            try:
                return await loader.load()
            except SourceUnavailable as exc:
                reasons.append(str(exc))
            # SourceRejected is deliberately NOT caught. A document that
            # arrived and is not what it claimed to be is a fact about this
            # publisher, and silently trying the next transport would hide it
            # behind whatever the fallback happened to hold.
        raise SourceUnavailable(
            f"{self.spec.name}: no transport produced a document — "
            + "; ".join(reasons or ["no loaders configured"])
        )

    def parse(self, document: HarvestDocument, *, retrieved_at: datetime) -> list[IntelClaim]:
        return parse_by_media(
            document, spec=self.spec, parsers=self._parsers, retrieved_at=retrieved_at
        )
