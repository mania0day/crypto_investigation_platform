"""Explorer name tags — an unverified LEAD channel, never an evidence channel.

The chain records behaviour, not ownership, so ``analysis.heuristics.service``
can prove an address *is* custodial infrastructure and still be unable to say
*whose*. Identity needs an off-chain list, and on Tron exactly one exchange
publishes a signed one (OKX). The consequence, measured on a live trace: 22
addresses correctly identified as exchange infrastructure, one of them one hop
from the root, and the report could only reach OKX two hops away.

A public explorer knows some of those names. TronScan tags the same addresses a
commercial product does — Bybit, MXC, WhiteBIT — free and keylessly. But a
curated explorer tag is not a signature and not a first-party publication, so
under ``intel.policy.TRUSTED_METHODS`` it is ``community``: it arrives
**pending**, and pending never reaches the attributor (``active_labels()`` is
the attributor's only load). That is the whole safety argument, and it is
enforced by machinery that already existed rather than by this module.

So what this module adds is deliberately small and deliberately one-way:

- It writes claims through :class:`~cipherchain.intel.policy.IntelClaim`, whose
  ``__post_init__`` already refuses the exact attack that closed community
  feeds — ``Binance (successor wallet 0xATTACKER)`` is rejected at
  construction for containing parentheses, before it can stem to "binance".
- What it writes is a label row, so a name fetched once is cached for every
  later investigation instead of re-asked per trace.
- Nothing here builds a Finding or an Evidence. An unverified tag rides on the
  graph NODE (``GraphNodeOut.unverified_tags``) and stops there. It cannot be
  cited, cannot answer an objective, and cannot become "nearest NAMED endpoint".

Failure is always soft. A trace that cannot reach an explorer is a trace with
fewer leads, never a failed trace.
"""

from __future__ import annotations

import asyncio
import logging
import os
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Protocol

import httpx

from cipherchain.intel.policy import IntelClaim

logger = logging.getLogger(__name__)

#: Marks every row this module writes. Claim identity is (chain, address,
#: source), so one explorer can never corroborate itself no matter how many
#: times it is asked.
TRONSCAN_SOURCE = "tronscan-public-tag"

#: ``community`` is the point: ``arrival_status`` maps it to 'pending'.
TAG_METHOD = "community"

#: Confidence must sit inside the open interval (0, 1) — the labels table has a
#: check constraint. Deliberately low: this is the weakest claim the system
#: accepts, and it is displayed as a lead rather than ranked against real ones.
TAG_CONFIDENCE = 0.30

#: The keyless legacy endpoint. ``accountv2`` is the documented one but answers
#: 401 without a TronScan key; when ``TRONSCAN_API_KEY`` is set we use it.
_TRONSCAN_LEGACY = "https://apilist.tronscanapi.com/api/account"
_TRONSCAN_V2 = "https://apilist.tronscanapi.com/api/accountv2"

#: A free public API asked politely: one lookup at a time, spaced, and capped
#: per investigation. The cap is why enrichment targets suspected endpoints
#: rather than every unlabelled node — 22 requests, not 1,849.
REQUEST_SPACING_SECONDS = 1.2
DEFAULT_MAX_LOOKUPS = 40
REQUEST_TIMEOUT_SECONDS = 20.0


class TagReader(Protocol):
    """One explorer's "what do you call this address?" reader."""

    async def __call__(
        self, address: str, *, http: httpx.AsyncClient
    ) -> ExplorerTag | None: ...  # pragma: no cover - structural type


@dataclass(frozen=True, slots=True)
class ExplorerTag:
    """One name an explorer puts on one address. Not evidence of anything."""

    chain: str
    address: str
    tag: str
    source: str


def _clean_tag(raw: object) -> str | None:
    """Normalise an explorer's tag field into a name, or reject it.

    Tag text is attacker-influenceable in the general case, so it is treated as
    untrusted input here and again at :class:`IntelClaim`. This pass only
    handles shape — collapse whitespace, drop empties.

    Note what this deliberately does NOT do: judge the *category*. An explorer
    tag names an entity; it does not say what kind of entity. That is why
    enrichment only ever targets addresses that already carry a behavioural
    service-endpoint finding — ``category='vasp'`` is then justified by our own
    heuristic, and the explorer contributes nothing but the NAME. An explorer
    is never allowed to decide what something is.
    """
    if not isinstance(raw, str):
        return None
    collapsed = " ".join(raw.split())
    return collapsed or None


async def fetch_tron_tag(
    address: str,
    *,
    http: httpx.AsyncClient,
    api_key: str | None = None,
) -> ExplorerTag | None:
    """Ask TronScan what it calls ``address``. ``None`` for "it doesn't".

    Never raises: an explorer being down, slow, rate-limiting, or returning
    something unparseable all mean the same thing to the caller — no lead.
    """
    key = api_key if api_key is not None else os.environ.get("TRONSCAN_API_KEY")
    url = _TRONSCAN_V2 if key else _TRONSCAN_LEGACY
    headers = {"TRON-PRO-API-KEY": key} if key else {}
    try:
        response = await http.get(
            url,
            params={"address": address},
            headers=headers,
            timeout=REQUEST_TIMEOUT_SECONDS,
        )
        if response.status_code != 200:
            logger.debug("tronscan tag lookup %s: HTTP %d", address, response.status_code)
            return None
        payload = response.json()
    except (httpx.HTTPError, ValueError) as exc:
        logger.debug("tronscan tag lookup %s failed: %s", address, exc)
        return None
    if not isinstance(payload, dict):
        return None
    tag = _clean_tag(payload.get("addressTag"))
    if tag is None:
        return None
    return ExplorerTag(chain="tron", address=address, tag=tag, source=TRONSCAN_SOURCE)


def claim_from_tag(tag: ExplorerTag, *, now: datetime | None = None) -> IntelClaim | None:
    """Turn a tag into a pending claim, or ``None`` if the tag is unusable.

    ``IntelClaim.__post_init__`` raises on an entity that breaks the untrusted
    -claim shape rules. That is a rejection, not a bug, so it is caught and
    logged here: one hostile or malformed tag must not abort enrichment for the
    other twenty-one addresses.
    """
    try:
        return IntelClaim(
            chain=tag.chain,
            address=tag.address,
            entity=tag.tag,
            category="vasp",
            role="unknown",
            confidence=TAG_CONFIDENCE,
            method=TAG_METHOD,
            source=tag.source,
            retrieved_at=now or datetime.now(UTC),
        )
    except ValueError as exc:
        logger.info("rejected explorer tag for %s: %s", tag.address, exc)
        return None


#: Chain -> the reader that can name addresses on it. ``SUPPORTED_CHAINS`` is
#: DERIVED from this rather than written alongside it, so a chain can never be
#: declared supported without a reader — the version where the two were
#: separate constants would have sent Ethereum addresses to a Tron endpoint and
#: reported "the explorer knew nobody".
READERS: dict[str, TagReader] = {"tron": fetch_tron_tag}
SUPPORTED_CHAINS = frozenset(READERS)


async def lookup_tags(
    addresses: list[str],
    *,
    chain: str,
    http: httpx.AsyncClient,
    max_lookups: int = DEFAULT_MAX_LOOKUPS,
    spacing: float = REQUEST_SPACING_SECONDS,
) -> list[ExplorerTag]:
    """Look up a bounded, rate-limited batch. Order preserved; misses dropped.

    Sequential on purpose. Concurrency here would buy a second or two and cost
    the goodwill of a free public API that owes us nothing.
    """
    reader = READERS.get(chain)
    if reader is None:
        return []
    found: list[ExplorerTag] = []
    for index, address in enumerate(addresses[:max_lookups]):
        if index:
            await asyncio.sleep(spacing)
        tag = await reader(address, http=http)
        if tag is not None:
            found.append(tag)
    if len(addresses) > max_lookups:
        # Never let a cap read as coverage. The caller reports this.
        logger.info(
            "explorer tag lookup capped: asked %d of %d addresses",
            max_lookups,
            len(addresses),
        )
    return found
