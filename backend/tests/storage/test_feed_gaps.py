"""A lost acquisition feed has to name itself, all the way to the wire.

Losing a feed is the ROUTINE way this system degrades once its keyed provider
quotas are spent: the trace keeps moving on whatever tier still answers and
pays for it in the rows that tier could not serve. ``HistoryPage.gaps`` carries
the loss out of the adapter and the engine records it — but for a while it
recorded only ``history_truncated``, which is deliberately the union of three
unrelated limits and therefore answers "was this address read in full?" while
losing the only question a reader can act on: WHAT is missing.

These tests hold that distinction open. An address whose every ETH transfer is
present and whose USDT transfers are absent is a different piece of evidence
from one whose page was merely cut short, and the difference has to survive the
database, a resumed run, the report and the API response.
"""

import uuid

import pytest
from sqlalchemy import text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from cipherchain.chains.base import FeedGap, feed_name_for_code
from cipherchain.core.models import Address, Capability, Direction
from cipherchain.reporting.model import TraversalCoverage, _coverage_caveats
from cipherchain.storage.repositories import FactRepository, InvestigationRepository

TOKEN_CODE = FeedGap(chain="ethereum", capability=Capability.TOKEN_TRANSFERS).code
INTERNAL_CODE = FeedGap(chain="ethereum", capability=Capability.INTERNAL_TRACES).code


async def new_node(
    session: AsyncSession, address: str
) -> tuple[InvestigationRepository, uuid.UUID, int]:
    facts = FactRepository(session)
    root_id = await facts.get_or_create_address(Address("ethereum", address))
    repo = InvestigationRepository(session)
    row = await repo.create(
        root_address_id=root_id,
        objectives=["find_prev_vasp"],
        budgets={"api_calls": 100, "seconds": 300, "max_depth": 4, "max_nodes": 500},
        engine_version="0.1.0",
        ruleset_version="2026-08-16",
    )
    node_id = await repo.add_address_node(
        row.id,
        root_id,
        direction=Direction.BACKWARD,
        hop_distance=1,
        value_share=10,
        discovered_reason="find_prev_vasp",
    )
    assert node_id is not None
    return repo, row.id, node_id


async def test_the_feed_that_was_lost_is_recorded_by_name(session: AsyncSession) -> None:
    repo, investigation_id, node_id = await new_node(session, "0xaa")
    await repo.mark_feed_unavailable(node_id, TOKEN_CODE)

    nodes, codes = await repo.count_nodes_missing_feeds(investigation_id)
    assert nodes == 1
    assert codes == (TOKEN_CODE,)
    # The point of storing the code at all: it reads back as the money that went
    # missing, not as a provider's constant.
    assert feed_name_for_code(codes[0]) == "token transfers"


async def test_two_lost_feeds_on_one_address_are_one_address_and_two_feeds(
    session: AsyncSession,
) -> None:
    repo, investigation_id, node_id = await new_node(session, "0xbb")
    await repo.mark_feed_unavailable(node_id, TOKEN_CODE)
    await repo.mark_feed_unavailable(node_id, INTERNAL_CODE)

    nodes, codes = await repo.count_nodes_missing_feeds(investigation_id)
    assert nodes == 1, "one address lost two feeds; that is one partially-read address"
    assert set(codes) == {TOKEN_CODE, INTERNAL_CODE}


async def test_recording_the_same_feed_twice_does_not_grow_the_gap(session: AsyncSession) -> None:
    """A resumed run re-reads addresses the first run already recorded.

    Without idempotence the coverage figure grows every time an investigation is
    resumed, which turns a durable record into a counter of how often it was
    read — and the whole reason this lives in a column is to survive a resume.
    """
    repo, investigation_id, node_id = await new_node(session, "0xcc")
    for _ in range(3):
        await repo.mark_feed_unavailable(node_id, TOKEN_CODE)

    nodes, codes = await repo.count_nodes_missing_feeds(investigation_id)
    assert nodes == 1
    assert codes == (TOKEN_CODE,)
    stored = (
        await session.execute(
            text("SELECT feeds_unavailable FROM nodes WHERE id = :i"), {"i": node_id}
        )
    ).scalar_one()
    assert stored == [TOKEN_CODE], "the code was appended more than once"


async def test_an_address_that_lost_nothing_records_nothing(session: AsyncSession) -> None:
    repo, investigation_id, node_id = await new_node(session, "0xdd")
    nodes, codes = await repo.count_nodes_missing_feeds(investigation_id)
    assert (nodes, codes) == (0, ())
    stored = (
        await session.execute(
            text("SELECT feeds_unavailable FROM nodes WHERE id = :i"), {"i": node_id}
        )
    ).scalar_one()
    assert stored is None, "NULL is how 'every feed answered' is said"


async def test_a_gap_must_name_a_feed(session: AsyncSession) -> None:
    repo, _, node_id = await new_node(session, "0xee")
    with pytest.raises(ValueError, match="must name"):
        await repo.mark_feed_unavailable(node_id, "")


async def test_the_database_refuses_a_gap_with_no_feeds_in_it(session: AsyncSession) -> None:
    """An empty array is a recorded gap that names nothing.

    Downstream it counts as an address with a gap whose name nobody can print —
    which is exactly the state this column was added to remove, so the database
    holds the line rather than trusting every future writer.
    """
    _, _, node_id = await new_node(session, "0xff")
    with pytest.raises(IntegrityError):
        await session.execute(
            text("UPDATE nodes SET feeds_unavailable = '[]'::jsonb WHERE id = :i"), {"i": node_id}
        )
    await session.rollback()


def test_a_run_that_lost_a_feed_is_not_complete() -> None:
    """The one lie this whole path exists to prevent."""
    lost = TraversalCoverage(
        addresses_reached=3, addresses_missing_feeds=1, feeds_unavailable=(TOKEN_CODE,)
    )
    assert not lost.complete
    assert TraversalCoverage(addresses_reached=3).complete


def test_the_caveat_names_the_feed_a_reader_lost() -> None:
    caveats = _coverage_caveats(
        TraversalCoverage(
            addresses_reached=3, addresses_missing_feeds=2, feeds_unavailable=(TOKEN_CODE,)
        )
    )
    feed = [c for c in caveats if c.code == "feed_unavailable"]
    assert len(feed) == 1
    # Named in the reader's terms, and the consequence stated: silence in a
    # missing feed is not evidence of absence.
    assert "token transfers" in feed[0].headline
    assert "2 address(es)" in feed[0].headline
    assert "not evidence that none" in feed[0].detail


def test_an_unknown_feed_code_still_prints_something() -> None:
    """A renamed capability must not delete the caveat that names it."""
    assert feed_name_for_code("feed_unavailable:something_new") == "feed_unavailable:something_new"
