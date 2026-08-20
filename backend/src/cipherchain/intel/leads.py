"""Name leads for endpoints behaviour identified but no source could name.

The gap this closes, measured on investigation 2304dde8 (a live Tron trace,
1,849 nodes): the walk correctly identified 22 addresses as exchange
infrastructure — including one a single hop from the root — and could name none
of them, because the Tron label store holds exactly one nameable exchange. The
report's nearest NAMED endpoint sat two hops further out than its nearest
*reached* one.

A public explorer knows some of those names. Asking it turns "operator unnamed"
into "operator unnamed; TronScan calls this Bybit" — which is a lead an
investigator can act on (subpoena the right exchange) without it being a
finding anyone can cite.

Three invariants hold this honest, and none of them are new code:

1. The claim is written with method ``community``, so
   :func:`~cipherchain.intel.policy.arrival_status` files it **pending**.
2. The attributor loads ``active_labels()`` and nothing else, so a pending row
   is structurally incapable of naming an endpoint, answering an objective, or
   appearing as evidence.
3. :class:`~cipherchain.intel.policy.IntelClaim` rejects the entity shapes that
   made community feeds dangerous — the ``Binance (successor wallet 0x…)``
   smuggling channel is refused at construction.

The worklist comes from
:meth:`~cipherchain.storage.repositories.InvestigationRepository.unnamed_service_endpoints`,
which is *already* restricted to addresses our own heuristic called custodial.
That is what justifies filing the claim as ``category='vasp'``: the category is
ours, from behaviour; the explorer contributes only the name.
"""

from __future__ import annotations

import logging
import uuid
from dataclasses import dataclass, field

import httpx
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from cipherchain.intel.explorer_tags import (
    DEFAULT_MAX_LOOKUPS,
    SUPPORTED_CHAINS,
    ExplorerTag,
    claim_from_tag,
    lookup_tags,
)
from cipherchain.intel.service import IntelService
from cipherchain.storage.repositories import InvestigationRepository

logger = logging.getLogger(__name__)

__all__ = ["SUPPORTED_CHAINS", "LeadResult", "enrich_investigation"]


@dataclass(slots=True)
class LeadResult:
    """What one enrichment pass did — including what it did NOT do.

    ``examined`` vs ``candidates`` is the honesty field: when the cap bites,
    the difference is addresses nobody asked about, and a UI that reported only
    ``named`` would present a truncated sweep as a complete one.
    """

    candidates: int = 0
    examined: int = 0
    named: int = 0
    unsupported_chains: set[str] = field(default_factory=set)
    tags: list[ExplorerTag] = field(default_factory=list)

    @property
    def capped(self) -> bool:
        return self.examined < self.candidates


async def enrich_investigation(
    investigation_id: uuid.UUID,
    *,
    session_factory: async_sessionmaker[AsyncSession],
    http: httpx.AsyncClient,
    max_lookups: int = DEFAULT_MAX_LOOKUPS,
) -> LeadResult:
    """Fetch explorer names for this run's unnamed endpoints. Never raises.

    Runs entirely outside the traversal: the engine's budget clock has already
    stopped, so a slow explorer costs an investigator seconds of waiting and
    can never change which addresses a trace opened. That separation is the
    reason this is a post-run pass and not a step inside the walk.
    """
    result = LeadResult()
    async with session_factory() as session:
        endpoints = await InvestigationRepository(session).unnamed_service_endpoints(
            investigation_id
        )
    if not endpoints:
        return result

    by_chain: dict[str, list[str]] = {}
    for chain, address in endpoints:
        if chain not in SUPPORTED_CHAINS:
            result.unsupported_chains.add(chain)
            continue
        by_chain.setdefault(chain, []).append(address)
    result.candidates = sum(len(v) for v in by_chain.values())

    for chain, addresses in by_chain.items():
        result.examined += min(len(addresses), max_lookups)
        tags = await lookup_tags(addresses, chain=chain, http=http, max_lookups=max_lookups)
        result.tags.extend(tags)
        if not tags:
            continue
        async with session_factory() as session:
            intel = IntelService(session)
            for tag in tags:
                claim = claim_from_tag(tag)
                if claim is None:
                    continue
                try:
                    await intel.ingest(claim)
                    result.named += 1
                except Exception:  # pragma: no cover - defensive
                    # One bad claim must not lose the other twenty-one.
                    logger.exception("could not record explorer tag for %s", tag.address)
            await session.commit()

    logger.info(
        "lead enrichment %s: %d candidate(s), %d examined, %d named%s",
        investigation_id,
        result.candidates,
        result.examined,
        result.named,
        " (CAPPED)" if result.capped else "",
    )
    return result
