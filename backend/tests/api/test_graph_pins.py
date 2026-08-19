"""The graph must be able to show the address the report's headline names.

On investigation ba0783b9 it could not: the answer named a previous VASP whose
node ranked 22nd of 96 by value share inside its (hop, direction) level, the
level budget is 20, and so the endpoint of the whole investigation was the one
address missing from the picture of it.

The ledger here is that shape in miniature — a wide funding hop whose VASP is
its SMALLEST contributor, so value share ranks it last and only the finding
filed against it keeps it on screen.

Keeping the node is only half of it. ba0783b9's endpoint sits at hop 2 behind a
hop-1 parent nobody has labelled, and an edge is drawn only when both its
endpoints are on screen — so ``DEEP_FUNDING`` puts the pin one hop further out
than the budget reaches, where keeping it and joining it up are different
questions.
"""

from __future__ import annotations

from collections import defaultdict
from typing import Any

import httpx
import pytest
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from cipherchain.api.app import GRAPH_NODE_MAX
from cipherchain.chains.base import ChainRegistry
from cipherchain.investigation import InvestigationEngine
from tests.api.conftest import VASP_LABELS
from tests.investigation.conftest import (
    CHAIN,
    EXCHANGE_IN,
    ROOT,
    FakeAdapter,
    Hop,
    MapAttributor,
)

WHALE = "whale_funder"
MIDDLING = "middling_funder"
QUIET = "quiet_funder"

# One hop backward, three funders, and the exchange pays the least of them.
WIDE_FUNDING: tuple[Hop, ...] = (
    Hop(WHALE, ROOT, 900, 1, "tx_whale"),
    Hop(MIDDLING, ROOT, 500, 2, "tx_middling"),
    Hop(EXCHANGE_IN, ROOT, 10, 3, "tx_ex_in"),
)

# The same shape one hop deeper: the exchange funded a nobody, the nobody funded
# the subject, and a whale outranks the nobody at hop 1. The pin then sits at hop
# 2 and the only node that can join it to the trace is one the budget threw away.
DEEP_FUNDING: tuple[Hop, ...] = (
    Hop(WHALE, ROOT, 900, 1, "tx_whale"),
    Hop(QUIET, ROOT, 10, 2, "tx_quiet"),
    Hop(EXCHANGE_IN, QUIET, 10, 3, "tx_ex_in"),
)


def joined_to_the_root(graph: dict[str, Any]) -> set[str]:
    """Addresses a reader can trace back to the subject along the drawn edges.

    Undirected, because that is what a reader sees: a node is orphaned when no
    line on the screen connects it to the address under investigation, whichever
    way the arrow points.
    """
    neighbours: dict[int, list[int]] = defaultdict(list)
    for edge in graph["edges"]:
        neighbours[edge["src"]].append(edge["dst"])
        neighbours[edge["dst"]].append(edge["src"])
    (root_id,) = [node["id"] for node in graph["nodes"] if node["hop"] == 0]
    seen = {root_id}
    frontier = [root_id]
    while frontier:
        for neighbour in neighbours[frontier.pop()]:
            if neighbour not in seen:
                seen.add(neighbour)
                frontier.append(neighbour)
    return {node["address"] for node in graph["nodes"] if node["id"] in seen}


@pytest.fixture
def investigation_engine(
    sessions: async_sessionmaker[AsyncSession],
) -> InvestigationEngine:
    registry = ChainRegistry()
    registry.register(FakeAdapter(ledger=WIDE_FUNDING))
    return InvestigationEngine(registry, sessions, MapAttributor(VASP_LABELS))


async def start_trace(client: httpx.AsyncClient) -> str:
    started = await client.post(
        "/investigations",
        json={"chain": CHAIN, "address": ROOT, "objectives": ["find_prev_vasp"]},
    )
    assert started.status_code == 201, started.text
    return str(started.json()["investigation_id"])


async def test_a_tight_level_budget_still_draws_the_named_endpoint(
    client: httpx.AsyncClient,
) -> None:
    investigation_id = await start_trace(client)
    findings = (await client.get(f"/investigations/{investigation_id}/findings")).json()
    assert [a["nearest"]["address"] for a in findings["answers"]] == [EXCHANGE_IN], (
        "fixture must actually name the exchange, or this proves nothing"
    )

    graph = (await client.get(f"/investigations/{investigation_id}/graph?per_level=1")).json()
    addresses = [node["address"] for node in graph["nodes"]]
    assert EXCHANGE_IN in addresses, "the answer's own address was cut from the picture"
    # The budget still binds every ordinary node: the top funder of the level
    # wins the single slot and the second one does not get in behind the pin.
    assert WHALE in addresses
    assert MIDDLING not in addresses


async def test_pins_do_not_duplicate_a_node_or_dishonour_the_total(
    client: httpx.AsyncClient,
) -> None:
    investigation_id = await start_trace(client)
    graph = (await client.get(f"/investigations/{investigation_id}/graph?per_level=1")).json()

    ids = [node["id"] for node in graph["nodes"]]
    assert len(ids) == len(set(ids)), "a pinned node that also won its slot came back twice"
    assert graph["node_total"] == 4, "root plus three funders"
    assert len(graph["nodes"]) < graph["node_total"]
    assert graph["truncated"] is True

    whole = (await client.get(f"/investigations/{investigation_id}/graph")).json()
    assert len(whole["nodes"]) == whole["node_total"]
    assert whole["truncated"] is False, "nothing was dropped and nothing may claim it was"


class TestTheAnswerIsDrawnJoinedToTheTrace:
    """The endpoint two hops out, behind a parent no source has ever named.

    This is the live shape and the one the fixtures above cannot reach: they pin
    at hop 1, whose parent is the root, so the picture is connected however the
    quota falls. Move the pin one hop out and keeping it is no longer enough —
    the parent is an ordinary node that lost to a whale, and without it the
    address the report names is drawn beside the investigation rather than in it.
    """

    @pytest.fixture
    def investigation_engine(
        self, sessions: async_sessionmaker[AsyncSession]
    ) -> InvestigationEngine:
        registry = ChainRegistry()
        registry.register(FakeAdapter(ledger=DEEP_FUNDING))
        return InvestigationEngine(registry, sessions, MapAttributor(VASP_LABELS))

    async def test_the_endpoints_parent_is_drawn_with_it(self, client: httpx.AsyncClient) -> None:
        investigation_id = await start_trace(client)
        findings = (await client.get(f"/investigations/{investigation_id}/findings")).json()
        assert [a["nearest"]["address"] for a in findings["answers"]] == [EXCHANGE_IN], (
            "fixture must actually name the exchange, or this proves nothing"
        )

        graph = (await client.get(f"/investigations/{investigation_id}/graph?per_level=1")).json()
        addresses = {node["address"] for node in graph["nodes"]}
        assert EXCHANGE_IN in addresses, "the answer's own address was cut from the picture"
        assert WHALE in addresses, "the quota still goes to the largest counterparty"
        assert QUIET in addresses, "the endpoint's parent lost its slot and was not brought back"
        assert EXCHANGE_IN in joined_to_the_root(graph), (
            "the endpoint was drawn as a loose dot, with no line back to the subject"
        )


def test_the_node_ceiling_admits_a_whole_real_trace() -> None:
    """A caller asking for the entire graph is asking for an export, not a
    picture, and 1000 quietly refused: ba0783b9 holds ~1600 address nodes, so
    the maximum a caller could ask for returned two thirds of the trace.
    """
    assert GRAPH_NODE_MAX >= 1600
