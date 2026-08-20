"""Lead enrichment against real storage — and the invariant it must not break.

The feature exists because a live Tron trace identified 22 addresses as
exchange infrastructure and could name none of them: the Tron label store holds
one nameable exchange, so the report's nearest NAMED endpoint sat two hops
beyond its nearest reached one. Asking a public explorer fills some of those
names in.

Every test here is really about one line: a name that arrives this way must be
visible to an investigator and invisible to everything that concludes.
``test_a_fetched_name_never_reaches_the_attributor`` is the load-bearing one.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime

import httpx
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from cipherchain.core.models import (
    Address,
    Direction,
    Evidence,
    EvidenceKind,
    Finding,
    FindingKind,
)
from cipherchain.intel.leads import enrich_investigation
from cipherchain.storage.repositories import (
    FactRepository,
    InvestigationRepository,
    LabelRepository,
)

CHAIN = "tron"
T0 = datetime(2026, 8, 20, 12, 0, tzinfo=UTC)
BYBIT = "TU4vEruvZwLLkSfV9bNw12EJTPvNr7Pvaa"
MXC = "TEPSrSYPDSQ7yXpMFPq91Fb1QEWpMkRGfn"
UNKNOWN = "TCDBeXbm9idyDtPy8SqWQkvNRmt3naUd3w"
TAGS = {BYBIT: "Bybit", MXC: "MXC"}


def tag_server(seen: list[str] | None = None) -> httpx.MockTransport:
    def handler(request: httpx.Request) -> httpx.Response:
        address = request.url.params["address"]
        if seen is not None:
            seen.append(address)
        return httpx.Response(200, json={"addressTag": TAGS.get(address, "")})

    return httpx.MockTransport(handler)


async def a_trace(session: AsyncSession, addresses: list[str], *, chain: str = CHAIN) -> uuid.UUID:
    """An investigation whose endpoints were found by BEHAVIOUR, not by label.

    Mirrors what the engine writes when nothing names an operator: a
    ``vasp_endpoint`` finding carrying a heuristic inference and no third-party
    claim.
    """
    facts = FactRepository(session)
    repo = InvestigationRepository(session)
    root_id = await facts.get_or_create_address(Address(chain, "Troot"))
    row = await repo.create(
        root_address_id=root_id,
        objectives=["find_prev_vasp"],
        budgets={"api_calls": 100, "seconds": 300, "max_depth": 3, "max_nodes": 500},
        engine_version="0.1.0",
        ruleset_version="2026-08-16",
    )
    for address in addresses:
        address_id = await facts.get_or_create_address(Address(chain, address))
        await repo.add_address_node(
            row.id,
            address_id,
            direction=Direction.BACKWARD,
            hop_distance=1,
            value_share=1_000,
            discovered_reason="find_prev_vasp",
        )
        await repo.add_finding(
            row.id,
            Finding(
                kind=FindingKind.VASP_ENDPOINT,
                subject=Address(chain=chain, value=address),
                summary="service endpoint (operator unnamed): collects from 144 …",
                confidence=0.75,
                evidence=(
                    Evidence(
                        kind=EvidenceKind.HEURISTIC_INFERENCE,
                        summary="bidirectional counterparty degree exceeds thresholds",
                        heuristic="service-endpoint@1",
                        confidence=0.75,
                    ),
                ),
                direction=Direction.BACKWARD,
            ),
            subject_address_id=address_id,
        )
    await session.commit()
    return row.id


# ------------------------------------------------------------- the worklist


async def test_worklist_is_the_endpoints_nothing_names(session: AsyncSession) -> None:
    investigation_id = await a_trace(session, [BYBIT, MXC, UNKNOWN])
    found = await InvestigationRepository(session).unnamed_service_endpoints(investigation_id)
    assert sorted(a for _, a in found) == sorted([BYBIT, MXC, UNKNOWN])


async def test_an_already_named_endpoint_is_not_asked_about(session: AsyncSession) -> None:
    """The saving that makes this affordable, and a correctness point too.

    Re-asking an explorer about an address a signed first-party list already
    names spends a request to learn nothing — and would file a weaker competing
    claim against a stronger one.
    """
    investigation_id = await a_trace(session, [BYBIT, MXC])
    await LabelRepository(session).upsert_claim(
        chain=CHAIN,
        address=BYBIT,
        entity="OKX",
        category="vasp",
        role="operational",
        confidence=0.9,
        status="active",
        method="signature",
        source="okx-por",
        retrieved_at=T0,
    )
    await session.commit()
    found = await InvestigationRepository(session).unnamed_service_endpoints(investigation_id)
    assert [a for _, a in found] == [MXC]


async def test_a_pending_claim_does_not_count_as_named(session: AsyncSession) -> None:
    """Otherwise one pass would permanently suppress the next one.

    A pending row is the thing this feature WRITES. If pending counted as
    "named", a second lookup could never refresh or correct the first.
    """
    investigation_id = await a_trace(session, [BYBIT])
    await LabelRepository(session).upsert_claim(
        chain=CHAIN,
        address=BYBIT,
        entity="Bybit",
        category="vasp",
        role="unknown",
        confidence=0.3,
        status="pending",
        method="community",
        source="tronscan-public-tag",
        retrieved_at=T0,
    )
    await session.commit()
    found = await InvestigationRepository(session).unnamed_service_endpoints(investigation_id)
    assert [a for _, a in found] == [BYBIT]


# ------------------------------------------------------------- enrichment


async def test_enrichment_records_the_names_it_finds(
    session: AsyncSession, sessions: async_sessionmaker[AsyncSession]
) -> None:
    investigation_id = await a_trace(session, [BYBIT, MXC, UNKNOWN])
    async with httpx.AsyncClient(transport=tag_server()) as http:
        result = await enrich_investigation(investigation_id, session_factory=sessions, http=http)
    assert result.candidates == 3
    assert result.examined == 3
    assert result.named == 2
    assert not result.capped

    stored = await LabelRepository(session).pending_labels_for(CHAIN, [BYBIT, MXC, UNKNOWN])
    assert sorted(stored) == sorted([BYBIT, MXC])
    assert stored[BYBIT][0].entity == "Bybit"
    assert stored[MXC][0].entity == "MXC"
    # The address the explorer had no name for stays unnamed rather than
    # acquiring an empty or placeholder label.
    assert UNKNOWN not in stored


async def test_a_fetched_name_never_reaches_the_attributor(
    session: AsyncSession, sessions: async_sessionmaker[AsyncSession]
) -> None:
    """THE invariant. Everything else in this file is scaffolding for it.

    ``active_labels()`` is the attributor's only load, so a name that cannot
    appear there cannot name an endpoint, answer an objective, or be cited. The
    assertion is against the attributor's actual query rather than against the
    status column, because the status column is not what the engine reads.
    """
    investigation_id = await a_trace(session, [BYBIT, MXC])
    async with httpx.AsyncClient(transport=tag_server()) as http:
        await enrich_investigation(investigation_id, session_factory=sessions, http=http)

    labels = LabelRepository(session)
    active = await labels.active_labels()
    assert [row for row in active if row.source == "tronscan-public-tag"] == []
    assert {row.address for row in await labels.pending_labels()} == {BYBIT, MXC}


async def test_capped_pass_reports_that_it_was_capped(
    session: AsyncSession, sessions: async_sessionmaker[AsyncSession]
) -> None:
    """A truncated sweep must never read as a complete one."""
    investigation_id = await a_trace(session, [BYBIT, MXC, UNKNOWN])
    async with httpx.AsyncClient(transport=tag_server()) as http:
        result = await enrich_investigation(
            investigation_id, session_factory=sessions, http=http, max_lookups=1
        )
    assert result.capped
    assert result.candidates == 3
    assert result.examined == 1


async def test_unsupported_chain_is_reported_not_silently_empty(
    session: AsyncSession, sessions: async_sessionmaker[AsyncSession]
) -> None:
    """ "No reader for this chain" and "the explorer knew nobody" differ."""
    investigation_id = await a_trace(session, ["0xdeadbeef"], chain="ethereum")
    seen: list[str] = []
    async with httpx.AsyncClient(transport=tag_server(seen)) as http:
        result = await enrich_investigation(investigation_id, session_factory=sessions, http=http)
    assert result.unsupported_chains == {"ethereum"}
    assert result.candidates == 0
    assert seen == []


async def test_an_explorer_that_is_down_costs_leads_not_the_run(
    session: AsyncSession, sessions: async_sessionmaker[AsyncSession]
) -> None:
    investigation_id = await a_trace(session, [BYBIT, MXC])

    def dead(_request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("no route to host")

    async with httpx.AsyncClient(transport=httpx.MockTransport(dead)) as http:
        result = await enrich_investigation(investigation_id, session_factory=sessions, http=http)
    assert result.named == 0
    assert result.candidates == 2
    assert await LabelRepository(session).pending_labels() == []


async def test_a_poisoned_tag_does_not_lose_the_honest_ones(
    session: AsyncSession, sessions: async_sessionmaker[AsyncSession]
) -> None:
    """One hostile tag in a batch must not abort the other lookups.

    The refusal happens at IntelClaim; what this pins is that the refusal is
    survivable — the earlier shape would have raised out of the ingest loop and
    dropped every name after it.
    """
    investigation_id = await a_trace(session, [BYBIT, MXC])

    def poisoned(request: httpx.Request) -> httpx.Response:
        address = request.url.params["address"]
        tag = "Binance (successor wallet 0xATTACKER)" if address == BYBIT else "MXC"
        return httpx.Response(200, json={"addressTag": tag})

    async with httpx.AsyncClient(transport=httpx.MockTransport(poisoned)) as http:
        result = await enrich_investigation(investigation_id, session_factory=sessions, http=http)
    assert result.named == 1
    stored = await LabelRepository(session).pending_labels()
    assert [(row.address, row.entity) for row in stored] == [(MXC, "MXC")]


# ------------------------------------------------------------------- read path


async def test_pending_read_is_scoped_and_does_not_scan(session: AsyncSession) -> None:
    labels = LabelRepository(session)
    for address, entity in ((BYBIT, "Bybit"), (MXC, "MXC")):
        await labels.upsert_claim(
            chain=CHAIN,
            address=address,
            entity=entity,
            category="vasp",
            role="unknown",
            confidence=0.3,
            status="pending",
            method="community",
            source="tronscan-public-tag",
            retrieved_at=T0,
        )
    await session.commit()

    assert await labels.pending_labels_for(CHAIN, []) == {}
    assert set(await labels.pending_labels_for(CHAIN, [BYBIT])) == {BYBIT}
    assert await labels.pending_labels_for("ethereum", [BYBIT]) == {}
