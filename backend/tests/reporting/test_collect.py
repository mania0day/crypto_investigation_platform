"""Reading a stored investigation back out as a report.

Against a real database, because the parts that cannot be faked are exactly the
parts a report is wrong without: the hop distance that makes "nearest" mean
something, and the coverage counters that decide what the caveats say.
"""

from __future__ import annotations

import uuid

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from cipherchain.core.models import Address, Direction
from cipherchain.investigation.budgets import Budgets
from cipherchain.reporting.collect import ReportNotFound, collect_report
from cipherchain.reporting.html import render_html
from cipherchain.storage.repositories import FactRepository, InvestigationRepository
from tests.reporting.conftest import CHAIN, mixer_finding, vasp_finding

ROOT = "0xroot00000000000000000000000000000000001"
EXCHANGE = "0xbinance"


async def _stored_investigation(session: AsyncSession, *, status: str = "completed") -> uuid.UUID:
    """One investigation with an answer, a mixer contact and real coverage gaps."""
    facts = FactRepository(session)
    investigations = InvestigationRepository(session)
    root_id = await facts.get_or_create_address(Address(chain=CHAIN, value=ROOT))
    row = await investigations.create(
        root_address_id=root_id,
        objectives=["find_prev_vasp", "find_next_vasp"],
        budgets=Budgets(max_depth=4).to_dict(),
        engine_version="test-engine",
        ruleset_version="test-ruleset",
    )
    root_node = await investigations.add_address_node(
        row.id, root_id, direction=None, hop_distance=0, value_share=None, discovered_reason="root"
    )
    assert root_node is not None
    await investigations.set_node_state(root_node, "expanded")
    # The subject's own history was longer than one page — the trace read part
    # of it, which is the single most common gap a real run carries.
    await investigations.mark_history_truncated(root_node)

    exchange_id = await facts.get_or_create_address(Address(chain=CHAIN, value=EXCHANGE))
    exchange_node = await investigations.add_address_node(
        row.id,
        exchange_id,
        direction=Direction.BACKWARD,
        hop_distance=2,
        value_share=99,
        discovered_reason="find_prev_vasp",
    )
    assert exchange_node is not None
    await investigations.set_node_state(exchange_node, "terminal", reason="vasp")
    await investigations.add_finding(
        row.id, vasp_finding(EXCHANGE, named=True), subject_address_id=exchange_id
    )

    mixer_id = await facts.get_or_create_address(Address(chain=CHAIN, value="0xtornado"))
    horizon_node = await investigations.add_address_node(
        row.id,
        mixer_id,
        direction=Direction.FORWARD,
        hop_distance=4,
        value_share=5,
        discovered_reason="find_next_vasp",
    )
    assert horizon_node is not None
    await investigations.set_node_state(horizon_node, "terminal", reason="depth_horizon")
    await investigations.add_finding(row.id, mixer_finding(), subject_address_id=mixer_id)

    # One node left on the frontier — a lead the run reached and never read.
    unexplored_id = await facts.get_or_create_address(Address(chain=CHAIN, value="0xunexplored"))
    await investigations.add_address_node(
        row.id,
        unexplored_id,
        direction=Direction.FORWARD,
        hop_distance=1,
        value_share=1,
        discovered_reason="find_next_vasp",
    )
    await investigations.set_status(row.id, status)
    await session.commit()
    return row.id


async def test_a_stored_investigation_becomes_a_report_with_its_answer_and_its_gaps(
    session: AsyncSession,
) -> None:
    """The whole path: rows in, document out, nothing about the run invented."""
    investigation_id = await _stored_investigation(session)
    report = await collect_report(session, investigation_id)

    assert report.header.subject.value == ROOT
    assert report.header.engine_version == "test-engine"
    assert report.header.ruleset_version == "test-ruleset"
    assert report.header.investigation_id == str(investigation_id)

    backward = next(a for a in report.answers if a.direction is Direction.BACKWARD)
    # Hop comes from the traversal record, not from the finding — without that
    # join "nearest" would be insertion order wearing a number.
    assert backward.nearest_named is not None
    assert backward.nearest_named.address == EXCHANGE
    assert backward.nearest_named.hop == 2
    assert backward.nearest_named.entity == "Binance"

    assert report.coverage.addresses_reached == 4
    assert report.coverage.truncated_histories == 1
    assert report.coverage.depth_horizon_stops == 1
    assert report.coverage.unexplored_frontier == 1
    assert report.coverage.max_depth == 4

    codes = {c.code for c in report.caveats}
    assert {
        "truncated_history",
        "depth_horizon",
        "unexplored_frontier",
        "mixer_contact",
        "no_endpoint_forward",
    } <= codes
    assert "Coverage and caveats" in render_html(report)


async def test_a_feed_no_provider_could_serve_reaches_the_document_and_the_wire(
    session: AsyncSession,
) -> None:
    """The degradation path, end to end, naming the feed it lost.

    This is the state a run is in once its keyed provider quotas are spent: the
    fallback tier keeps the trace alive and cannot answer every feed. What makes
    that safe rather than silent is that the loss survives as a NAMED fact — a
    reader tracing a stablecoin payment has to be told it was the token feed
    that went missing, because for them the difference between that and a merely
    truncated page is whether the report's silence means anything.
    """
    from cipherchain.api.schemas import CoverageOut
    from cipherchain.chains.base import FeedGap
    from cipherchain.core.models import Capability

    investigation_id = await _stored_investigation(session)
    investigations = InvestigationRepository(session)
    node = (await investigations.graph_nodes(investigation_id))[0]
    code = FeedGap(chain=CHAIN, capability=Capability.TOKEN_TRANSFERS).code
    await investigations.mark_feed_unavailable(node.id, code)
    await session.commit()

    report = await collect_report(session, investigation_id)
    assert report.coverage.addresses_missing_feeds == 1
    assert report.coverage.feeds_unavailable == (code,)
    assert report.coverage.complete is False

    caveat = next(c for c in report.caveats if c.code == "feed_unavailable")
    assert "token transfers" in caveat.headline
    assert "token transfers" in render_html(report)

    # And on the wire, in the same terms, so a caller never has to parse prose
    # to find out which half of an address's history it is missing.
    wire = CoverageOut.of(report.coverage).model_dump()
    assert wire["addresses_missing_feeds"] == 1
    assert wire["feeds_unavailable"] == [code]
    assert wire["complete"] is False


async def test_a_partial_run_reports_itself_as_partial(session: AsyncSession) -> None:
    """Status is read from the record, never inferred from what was found."""
    investigation_id = await _stored_investigation(session, status="partial")
    report = await collect_report(session, investigation_id)
    assert report.header.is_partial is True
    assert report.caveats[0].code == "partial_run"
    assert "coverage partial" in render_html(report)


async def test_reference_data_is_attached_to_the_endpoint_it_belongs_to(
    session: AsyncSession,
) -> None:
    """The operator name comes off the claim, and the lookup gets all three keys."""
    investigation_id = await _stored_investigation(session)
    seen: list[tuple[str, str, str | None]] = []

    async def lookup(*, chain: str, address: str, entity: str | None) -> dict[str, str]:
        seen.append((chain, address, entity))
        return {"entity": entity or address, "jurisdiction": "Cayman Islands"}

    report = await collect_report(session, investigation_id, vasp_lookup=lookup)
    backward = next(a for a in report.answers if a.direction is Direction.BACKWARD)
    assert seen == [(CHAIN, EXCHANGE, "Binance")]
    assert backward.nearest_named is not None
    assert backward.nearest_named.vasp is not None
    assert backward.nearest_named.vasp.jurisdiction == "Cayman Islands"


async def test_an_unknown_investigation_has_no_report(session: AsyncSession) -> None:
    """The API edge turns this into a 404 rather than an empty document."""
    with pytest.raises(ReportNotFound):
        await collect_report(session, uuid.uuid4())
