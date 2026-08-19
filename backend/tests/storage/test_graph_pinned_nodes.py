"""The per-level quota must not be able to hide the node the answer names.

Measured on investigation ba0783b9: the report's headline named a previous VASP
at hop 2 backward, and that node ranked 22nd of 96 by value share inside its
(hop, direction) level. The graph endpoint budgets 20 per level, so it returned
the level without the one address the headline was about — the reader could not
look at the thing they were reading about.

Value share ranks ordinary nodes perfectly well. What it cannot do is rank the
node a reader came to see, so the two classes that carry investigative meaning —
an address some source has made a claim about, and an address a ``vasp_endpoint``
finding was filed against — are returned outside the quota entirely.

Exempting them buys two obligations, and both are tested here. The exemption is
unbounded, so it must not spend the GLOBAL budget in one hop and take the depth
back out of the picture; and a pin arrives with the chain of nodes back to the
root, because an edge is drawn only when both its endpoints are on screen and a
pin whose parent was cut is a dot with no line to anything.
"""

import re
import uuid
from collections import defaultdict
from datetime import UTC, datetime
from pathlib import Path

from sqlalchemy.ext.asyncio import AsyncSession

from cipherchain.api.app import GRAPH_NODE_LIMIT, GRAPH_PER_LEVEL
from cipherchain.core.models import (
    Address,
    Asset,
    AssetKind,
    Direction,
    Evidence,
    EvidenceKind,
    Finding,
    FindingKind,
    Movement,
    MovementKind,
    Provenance,
    TxRef,
)
from cipherchain.storage.repositories import (
    FactRepository,
    GraphEdge,
    GraphNode,
    InvestigationRepository,
    LabelRepository,
)

CHAIN = "ethereum"
HARVESTED = datetime(2026, 8, 18, 12, 0, tzinfo=UTC)
ETH = Asset(chain=CHAIN, kind=AssetKind.NATIVE, symbol="ETH", decimals=18)
PROV = Provenance(provider="test", retrieved_at=HARVESTED, payload_sha256="0" * 64)
# Mirrors the live defect: rank 22 of a level, a per-level budget of 5.
CROWD = 30
PER_LEVEL = 5
BELOW_THE_CUT = 21  # zero-based index → rank 22


async def new_investigation(
    session: AsyncSession,
) -> tuple[InvestigationRepository, uuid.UUID]:
    facts = FactRepository(session)
    root_id = await facts.get_or_create_address(Address(CHAIN, "0xroot"))
    repo = InvestigationRepository(session)
    row = await repo.create(
        root_address_id=root_id,
        objectives=["find_prev_vasp"],
        budgets={"api_calls": 100, "seconds": 300, "max_depth": 4, "max_nodes": 500},
        engine_version="0.1.0",
        ruleset_version="2026-08-16",
    )
    return repo, row.id


async def add_node(
    session: AsyncSession,
    repo: InvestigationRepository,
    investigation_id: uuid.UUID,
    address: str,
    *,
    hop_distance: int,
    value_share: int,
    direction: Direction = Direction.BACKWARD,
) -> int:
    address_id = await FactRepository(session).get_or_create_address(Address(CHAIN, address))
    node_id = await repo.add_address_node(
        investigation_id,
        address_id,
        direction=direction,
        hop_distance=hop_distance,
        value_share=value_share,
        discovered_reason="find_prev_vasp",
    )
    assert node_id is not None
    return node_id


async def crowded_level(
    session: AsyncSession,
    repo: InvestigationRepository,
    investigation_id: uuid.UUID,
    *,
    hop_distance: int = 2,
    size: int = CROWD,
) -> list[str]:
    """One (hop, direction) level of ``size`` nodes, value share descending.

    Rank is then the list index: element 0 is rank 1, element 21 is rank 22.
    """
    addresses = [f"0xhop{hop_distance}_{index:02d}" for index in range(size)]
    for index, address in enumerate(addresses):
        await add_node(
            session,
            repo,
            investigation_id,
            address,
            hop_distance=hop_distance,
            value_share=(size - index) * 1_000,
        )
    return addresses


async def claim(session: AsyncSession, address: str, *, source: str = "etherscan-tags") -> int:
    label_id, _ = await LabelRepository(session).upsert_claim(
        chain=CHAIN,
        address=address,
        entity="Acme Exchange",
        category="vasp",
        role="deposit",
        confidence=0.9,
        status="active",
        method="first_party_published",
        source=source,
        retrieved_at=HARVESTED,
    )
    return label_id


async def vasp_finding(
    session: AsyncSession,
    repo: InvestigationRepository,
    investigation_id: uuid.UUID,
    address: str,
    *,
    direction: Direction = Direction.BACKWARD,
) -> int:
    address_id = await FactRepository(session).get_or_create_address(Address(CHAIN, address))
    return await repo.add_finding(
        investigation_id,
        Finding(
            kind=FindingKind.VASP_ENDPOINT,
            subject=Address(chain=CHAIN, value=address),
            summary=f"nearest previous VASP: {address}",
            confidence=0.9,
            evidence=(
                Evidence(
                    kind=EvidenceKind.THIRD_PARTY_CLAIM,
                    summary="Acme Exchange (deposit address)",
                    source="etherscan-tags@2026-08-10",
                    confidence=0.9,
                ),
            ),
            direction=direction,
        ),
        subject_address_id=address_id,
    )


async def a_movement(session: AsyncSession, tx_hash: str) -> int:
    """One stored transfer, so an edge stands for something the trace saw."""
    facts = FactRepository(session)
    movement = Movement(
        tx=TxRef(chain=CHAIN, tx_hash=tx_hash, timestamp=HARVESTED, block_number=1),
        asset=ETH,
        amount=1,
        kind=MovementKind.NATIVE,
        from_address=Address(CHAIN, f"{tx_hash}_from"),
        to_address=Address(CHAIN, f"{tx_hash}_to"),
        index=0,
        provenance=PROV,
    )
    tx_id, _ = await facts.store_movements(movement.tx, [movement])
    (stored,) = await facts.movements_for_transaction(tx_id)
    return stored.id


async def root_node(
    session: AsyncSession, repo: InvestigationRepository, investigation_id: uuid.UUID
) -> int:
    """The hop-0 node, written the way the engine writes it: no direction."""
    address_id = await FactRepository(session).get_or_create_address(Address(CHAIN, "0xroot"))
    node_id = await repo.add_address_node(
        investigation_id,
        address_id,
        direction=None,
        hop_distance=0,
        value_share=None,
        discovered_reason="root",
    )
    assert node_id is not None
    return node_id


async def traced_node(
    session: AsyncSession,
    repo: InvestigationRepository,
    investigation_id: uuid.UUID,
    address: str,
    *,
    parent_id: int,
    hop_distance: int,
    value_share: int,
) -> int:
    """A node AND the edge the trace followed to reach it.

    Nodes alone are enough to test the quota, but not the picture: the graph is
    drawn from edges, and the engine always records one from the node it
    expanded to the node it discovered.
    """
    node_id = await add_node(
        session,
        repo,
        investigation_id,
        address,
        hop_distance=hop_distance,
        value_share=value_share,
    )
    await repo.add_edge(
        investigation_id,
        src_node_id=parent_id,
        dst_node_id=node_id,
        movement_id=await a_movement(session, f"0xtx_{address}"),
    )
    return node_id


async def traced_level(
    session: AsyncSession,
    repo: InvestigationRepository,
    investigation_id: uuid.UUID,
    *,
    parent_id: int,
    hop_distance: int,
    size: int,
) -> list[tuple[str, int]]:
    """One level of ``size`` nodes hanging off ``parent_id``, value descending."""
    level = []
    for index in range(size):
        address = f"0xhop{hop_distance}_{index:02d}"
        level.append(
            (
                address,
                await traced_node(
                    session,
                    repo,
                    investigation_id,
                    address,
                    parent_id=parent_id,
                    hop_distance=hop_distance,
                    value_share=(size - index) * 1_000,
                ),
            )
        )
    return level


async def read(
    repo: InvestigationRepository,
    investigation_id: uuid.UUID,
    *,
    limit: int | None = None,
    per_level: int = PER_LEVEL,
) -> list[str]:
    nodes = await repo.graph_nodes(investigation_id, limit=limit, per_level=per_level)
    ids = [node.id for node in nodes]
    assert len(ids) == len(set(ids)), "a node must never be drawn twice"
    return [node.address for node in nodes]


async def drawn(
    repo: InvestigationRepository,
    investigation_id: uuid.UUID,
    *,
    limit: int | None = None,
    per_level: int = PER_LEVEL,
) -> tuple[list[GraphNode], list[GraphEdge]]:
    """The picture the endpoint draws: kept nodes, and the edges they permit.

    ``get_graph`` passes the ids of the nodes it kept straight into
    ``graph_edges``, so reproducing that pairing here is the only way a test can
    see an orphaned node at all — read the nodes alone and a pin looks fine.
    """
    nodes = await repo.graph_nodes(investigation_id, limit=limit, per_level=per_level)
    edges = await repo.graph_edges(investigation_id, node_ids=[node.id for node in nodes])
    return nodes, edges


def joined_to_the_root(nodes: list[GraphNode], edges: list[GraphEdge], root_id: int) -> set[str]:
    """Addresses a reader can trace back to the root along the drawn edges.

    Undirected, because that is what a reader sees: a node is orphaned when no
    line on the screen connects it to the subject of the investigation, whichever
    way the arrow points.
    """
    neighbours: dict[int, list[int]] = defaultdict(list)
    for edge in edges:
        neighbours[edge.src_node_id].append(edge.dst_node_id)
        neighbours[edge.dst_node_id].append(edge.src_node_id)
    seen = {root_id}
    frontier = [root_id]
    while frontier:
        for neighbour in neighbours[frontier.pop()]:
            if neighbour not in seen:
                seen.add(neighbour)
                frontier.append(neighbour)
    return {node.address for node in nodes if node.id in seen}


def ui_graph_request() -> tuple[int, int]:
    """The limit and per-level the shipped page actually polls ``/graph`` with.

    Read out of ``static/index.html`` rather than restated, because the value of
    this pair is that it is the OTHER side of the contract: a test that hard-coded
    900/90 would keep passing on the day the page changed its mind, which is the
    one day it needs to fail. The asserts below are the point — a page that no
    longer states these numbers must break this test rather than silently fall
    back to the endpoint defaults nothing sends.
    """
    page = (Path(__file__).resolve().parents[2] / "static" / "index.html").read_text()
    found = dict(re.findall(r"const (GRAPH_LIMIT|GRAPH_PER_LEVEL)\s*=\s*(\d+)", page))
    assert found.keys() == {"GRAPH_LIMIT", "GRAPH_PER_LEVEL"}, f"page states {found}"
    assert "limit=" in page and "per_level=" in page, "the page no longer parameterises /graph"
    return int(found["GRAPH_LIMIT"]), int(found["GRAPH_PER_LEVEL"])


class TestTheNodeTheAnswerNamesSurvivesTheQuota:
    async def test_a_labelled_node_below_the_cut_comes_back_anyway(
        self, session: AsyncSession
    ) -> None:
        repo, investigation_id = await new_investigation(session)
        level = await crowded_level(session, repo, investigation_id)
        await claim(session, level[BELOW_THE_CUT])

        returned = await read(repo, investigation_id)
        assert level[BELOW_THE_CUT] in returned

    async def test_an_unlabelled_node_below_the_cut_is_still_dropped(
        self, session: AsyncSession
    ) -> None:
        """The exemption is for meaning, not a way around the budget."""
        repo, investigation_id = await new_investigation(session)
        level = await crowded_level(session, repo, investigation_id)
        await claim(session, level[BELOW_THE_CUT])

        returned = await read(repo, investigation_id)
        assert level[BELOW_THE_CUT + 1] not in returned
        assert len(returned) == PER_LEVEL + 1

    async def test_a_vasp_endpoint_subject_below_the_cut_comes_back_anyway(
        self, session: AsyncSession
    ) -> None:
        """The live failure exactly: the endpoint the headline named was ranked
        22nd of its level, so the picture drew the level without it."""
        repo, investigation_id = await new_investigation(session)
        level = await crowded_level(session, repo, investigation_id)
        await vasp_finding(session, repo, investigation_id, level[BELOW_THE_CUT])

        returned = await read(repo, investigation_id)
        assert level[BELOW_THE_CUT] in returned

    async def test_a_finding_from_another_investigation_pins_nothing_here(
        self, session: AsyncSession
    ) -> None:
        """Findings are per-run. One investigation's endpoint is not a reason to
        spend another's budget on the same address."""
        repo, investigation_id = await new_investigation(session)
        level = await crowded_level(session, repo, investigation_id)
        _, elsewhere = await new_investigation(session)
        await vasp_finding(session, repo, elsewhere, level[BELOW_THE_CUT])

        returned = await read(repo, investigation_id)
        assert level[BELOW_THE_CUT] not in returned

    async def test_a_tight_overall_limit_keeps_the_pinned_node(self, session: AsyncSession) -> None:
        """``limit`` still applies to a pinned node, so its position in the
        order is the whole guarantee. Ranked where it actually sits, ba0783b9's
        endpoint lands past row 200 of a rank-major read and any caller asking
        for fewer nodes than that loses it again."""
        repo, investigation_id = await new_investigation(session)
        levels = [
            await crowded_level(session, repo, investigation_id, hop_distance=hop)
            for hop in (1, 2, 3)
        ]
        await claim(session, levels[2][BELOW_THE_CUT])

        returned = await read(repo, investigation_id, limit=8)
        assert len(returned) == 8
        assert levels[2][BELOW_THE_CUT] in returned


class TestPinsAreCountedOnceAndCountedHonestly:
    async def test_a_pinned_node_that_also_wins_its_slot_appears_once(
        self, session: AsyncSession
    ) -> None:
        """A label on the top node of a level must not return it twice — the
        renderer would draw one address as two."""
        repo, investigation_id = await new_investigation(session)
        level = await crowded_level(session, repo, investigation_id)
        await claim(session, level[0])
        await vasp_finding(session, repo, investigation_id, level[0])

        returned = await read(repo, investigation_id)
        assert returned.count(level[0]) == 1
        assert len(returned) == PER_LEVEL

    async def test_two_sources_naming_one_address_return_one_node(
        self, session: AsyncSession
    ) -> None:
        """A label's identity is (chain, address, source): corroboration is a
        SECOND row. Joining to labels instead of asking whether one exists
        returns the node once per source."""
        repo, investigation_id = await new_investigation(session)
        level = await crowded_level(session, repo, investigation_id)
        await claim(session, level[BELOW_THE_CUT], source="etherscan-tags")
        await claim(session, level[BELOW_THE_CUT], source="chainabuse")

        returned = await read(repo, investigation_id)
        assert returned.count(level[BELOW_THE_CUT]) == 1

    async def test_the_totals_still_describe_what_was_left_out(self, session: AsyncSession) -> None:
        """``truncated`` is ``node_total > len(nodes)``, and pins have to leave
        that arithmetic true: they are ordinary address nodes of this
        investigation, counted like every other."""
        repo, investigation_id = await new_investigation(session)
        level = await crowded_level(session, repo, investigation_id)
        await claim(session, level[BELOW_THE_CUT])

        total = await repo.count_graph_nodes(investigation_id)
        assert total == CROWD
        returned = await read(repo, investigation_id)
        assert total > len(returned), "nodes were dropped and the total must say so"

        # And when nothing is dropped, nothing claims to be.
        everything = await read(repo, investigation_id, per_level=CROWD)
        assert len(everything) == total

    async def test_a_level_made_entirely_of_pins_is_not_a_truncated_read(
        self, session: AsyncSession
    ) -> None:
        """The other direction of the same arithmetic: if every node is pinned,
        every node comes back, and a total computed from the caps rather than
        from the rows returned would report a whole graph as partial."""
        repo, investigation_id = await new_investigation(session)
        level = await crowded_level(session, repo, investigation_id, size=8)
        for address in level:
            await claim(session, address)

        returned = await read(repo, investigation_id, per_level=2)
        assert len(returned) == await repo.count_graph_nodes(investigation_id)


class TestOrdinaryNodesStillObeyTheBudget:
    async def test_every_level_keeps_its_own_budget(self, session: AsyncSession) -> None:
        """The per-level cap is why the picture has depth at all: a flat cap was
        spent entirely on hop 1 and dropped all 202 nodes at hop 2. Pinning must
        not turn the budget back into a global one."""
        repo, investigation_id = await new_investigation(session)
        levels = {
            hop: await crowded_level(session, repo, investigation_id, hop_distance=hop)
            for hop in (1, 2, 3)
        }
        await claim(session, levels[3][BELOW_THE_CUT])

        returned = set(await read(repo, investigation_id))
        for hop, level in levels.items():
            kept = [address for address in level if address in returned]
            expected = PER_LEVEL + (1 if hop == 3 else 0)
            assert len(kept) == expected, f"hop {hop} kept {kept}"
            # The quota still goes to the top of the level by value share.
            assert set(level[:PER_LEVEL]) <= returned

    async def test_a_dense_pin_cluster_at_one_hop_does_not_wipe_the_deeper_ones(
        self, session: AsyncSession
    ) -> None:
        """The exemption must not spend the GLOBAL budget in one hop.

        ``per_level`` exists because a flat cap was spent entirely on the
        nearest hop and dropped all 202 nodes at hop 2. Sorting every pinned
        node ahead of every ordinary one did the same thing by another route:
        the pinned tier is unbounded, so a hop sitting next to a labelled
        cluster consumed the whole ``limit`` and the deeper hops came back
        empty.

        Run at the endpoint's OWN defaults, imported rather than restated, on a
        trace the size the defaults were chosen for: four levels of 300 with 250
        labelled addresses at hop 1 read {1: 240, 2: 0, 3: 0, 4: 0} — three
        quarters of the picture gone — where the same data on the code before
        pinning read {1: 15, 2: 15, 3: 15, 4: 15}. Scaled-down numbers hid it:
        the pins have to outnumber what one level is allowed before they can
        starve the rest.

        ``limit`` is passed on purpose: it is the only parameter that makes the
        ordering observable at all, and a test without it cannot see this.
        """
        repo, investigation_id = await new_investigation(session)
        levels = {
            hop: await crowded_level(session, repo, investigation_id, hop_distance=hop, size=300)
            for hop in (1, 2, 3, 4)
        }
        for address in levels[1][:250]:
            await claim(session, address)

        returned = set(
            await read(repo, investigation_id, limit=GRAPH_NODE_LIMIT, per_level=GRAPH_PER_LEVEL)
        )
        per_hop = {hop: len([a for a in level if a in returned]) for hop, level in levels.items()}
        assert all(kept > 0 for kept in per_hop.values()), f"a hop was wiped: {per_hop}"
        for hop, level in levels.items():
            assert set(level[:GRAPH_PER_LEVEL]) <= returned, (
                f"hop {hop} lost its own quota to another hop's pins: {per_hop}"
            )
        # The budget is spent, not merely survived: pins take what is left over
        # after every level has been served, which is what makes them a
        # supplement to the picture rather than a replacement for it.
        assert sum(per_hop.values()) == GRAPH_NODE_LIMIT

    async def test_low_value_pins_cannot_evict_the_top_node_of_a_deeper_hop(
        self, session: AsyncSession
    ) -> None:
        """The same defect in its quietest form. Pins carry no value ranking of
        their own, so a hop full of DUST that some source once named would, with
        the pins hoisted, outrank the largest counterparty of every hop behind
        it — the picture then drops the biggest flows to keep the smallest
        labelled ones."""
        repo, investigation_id = await new_investigation(session)
        levels = {
            hop: await crowded_level(session, repo, investigation_id, hop_distance=hop, size=50)
            for hop in (1, 2, 3)
        }
        for address in levels[1][-30:]:  # the 30 SMALLEST of hop 1
            await claim(session, address)

        returned = await read(repo, investigation_id, limit=25, per_level=10)
        assert levels[2][0] in returned, "the largest counterparty at hop 2 was dropped for dust"
        assert levels[3][0] in returned, "the largest counterparty at hop 3 was dropped for dust"

    async def test_carrying_the_ancestors_along_does_not_wipe_a_hop_either(
        self, session: AsyncSession
    ) -> None:
        """A pin's path is a SECOND unbounded exemption, and it starves the
        picture the same way if it is ordered carelessly.

        Every pin here sits at hop 3 on a chain of its own, so the paths alone
        are twice the size of the level they lead to. Sorting them ahead of
        everything — the obvious way to be sure a pin is never orphaned — reads
        {1: 40, 2: 7, 3: 0} on this fixture: the budget goes to the paths and
        the level they exist to reach comes back empty.
        """
        repo, investigation_id = await new_investigation(session)
        root = await root_node(session, repo, investigation_id)
        size = 40
        hop1 = await traced_level(
            session, repo, investigation_id, parent_id=root, hop_distance=1, size=size
        )
        hop2: list[tuple[str, int]] = []
        for index in range(size):
            address = f"0xhop2_{index:02d}"
            hop2.append(
                (
                    address,
                    await traced_node(
                        session,
                        repo,
                        investigation_id,
                        address,
                        parent_id=hop1[index][1],
                        hop_distance=2,
                        value_share=(size - index) * 1_000,
                    ),
                )
            )
        # Pin i hangs off the SMALLEST chain, so no ancestor of a pin the reader
        # is likely to see ever wins a slot on its own merits.
        pins = []
        for index in range(size):
            address = f"0xpin_{index:02d}"
            pins.append(address)
            await traced_node(
                session,
                repo,
                investigation_id,
                address,
                parent_id=hop2[size - 1 - index][1],
                hop_distance=3,
                value_share=(size - index) * 1_000,
            )
            await claim(session, address)

        returned = set(await read(repo, investigation_id, limit=48, per_level=GRAPH_PER_LEVEL))
        levels = {1: [a for a, _ in hop1], 2: [a for a, _ in hop2], 3: pins}
        per_hop = {hop: len([a for a in level if a in returned]) for hop, level in levels.items()}
        assert all(kept > 0 for kept in per_hop.values()), f"a hop was wiped: {per_hop}"
        for hop, level in levels.items():
            assert set(level[:3]) <= returned, (
                f"hop {hop} lost its own nodes to the paths: {per_hop}"
            )


class TestAPinArrivesWithThePathToIt:
    """A pinned node with no line to anything is not an answer, it is a dot.

    ``graph_edges`` keeps an edge only when BOTH its endpoints are on screen, so
    exempting a node from the quota without exempting the nodes that lead to it
    draws the address the report is about floating beside the trace that reached
    it. ba0783b9 is exactly that shape: the OKX endpoint is at hop 2 backward
    and its hop-1 parent carries neither a label nor a finding, so nothing else
    would have brought the parent back.
    """

    async def test_the_parent_that_lost_its_slot_comes_back_with_the_pin(
        self, session: AsyncSession
    ) -> None:
        repo, investigation_id = await new_investigation(session)
        root = await root_node(session, repo, investigation_id)
        hop1 = await traced_level(
            session, repo, investigation_id, parent_id=root, hop_distance=1, size=CROWD
        )
        parent, parent_id = hop1[-1]  # last of its level by value: the quota cuts it
        await traced_node(
            session,
            repo,
            investigation_id,
            "0xendpoint",
            parent_id=parent_id,
            hop_distance=2,
            value_share=10,
        )
        await vasp_finding(session, repo, investigation_id, "0xendpoint")

        nodes, edges = await drawn(repo, investigation_id)
        addresses = {node.address for node in nodes}
        assert "0xendpoint" in addresses, "the pin itself must survive, or this proves nothing"
        assert parent in addresses, "the pin's parent was cut and nothing brought it back"
        # The parent came back BECAUSE the pin needs it — its neighbour of
        # identical rank, leading nowhere, is still cut.
        assert hop1[-2][0] not in addresses
        assert "0xendpoint" in joined_to_the_root(nodes, edges, root), (
            "the endpoint was drawn with no path back to the address under investigation"
        )

    async def test_a_whole_chain_of_cut_parents_comes_back(self, session: AsyncSession) -> None:
        """One step up is not enough. A pin deeper than hop 2 stands on a chain,
        and a picture that restores only the immediate parent moves the floating
        dot one hop closer to the root without joining it to anything."""
        repo, investigation_id = await new_investigation(session)
        root = await root_node(session, repo, investigation_id)
        hop1 = await traced_level(
            session, repo, investigation_id, parent_id=root, hop_distance=1, size=CROWD
        )
        grandparent, grandparent_id = hop1[-1]
        await traced_level(
            session, repo, investigation_id, parent_id=hop1[0][1], hop_distance=2, size=CROWD
        )
        parent_id = await traced_node(
            session,
            repo,
            investigation_id,
            "0xquiet_parent",
            parent_id=grandparent_id,
            hop_distance=2,
            value_share=1,  # smallest of its level, so the quota cuts it too
        )
        await traced_node(
            session,
            repo,
            investigation_id,
            "0xendpoint",
            parent_id=parent_id,
            hop_distance=3,
            value_share=10,
        )
        await vasp_finding(session, repo, investigation_id, "0xendpoint")

        nodes, edges = await drawn(repo, investigation_id)
        addresses = {node.address for node in nodes}
        assert {"0xquiet_parent", grandparent} <= addresses, f"the chain was broken: {addresses}"
        assert "0xendpoint" in joined_to_the_root(nodes, edges, root)

    async def test_a_tight_limit_cannot_cut_the_path_in_the_middle(
        self, session: AsyncSession
    ) -> None:
        """The path has to be ORDERED with the pin, not merely selected with it.
        Restoring an ancestor at its own rank leaves it behind the pin in the
        read order, so every caller asking for fewer nodes gets the dot back."""
        repo, investigation_id = await new_investigation(session)
        root = await root_node(session, repo, investigation_id)
        hop1 = await traced_level(
            session, repo, investigation_id, parent_id=root, hop_distance=1, size=CROWD
        )
        parent, parent_id = hop1[-1]
        await traced_node(
            session,
            repo,
            investigation_id,
            "0xendpoint",
            parent_id=parent_id,
            hop_distance=2,
            value_share=10,
        )
        await vasp_finding(session, repo, investigation_id, "0xendpoint")

        nodes, edges = await drawn(repo, investigation_id, limit=4)
        assert {node.address for node in nodes} == {"0xroot", hop1[0][0], parent, "0xendpoint"}, (
            "the four nodes a limit of four can afford are the answer, its path, "
            "and the biggest counterparty"
        )
        assert "0xendpoint" in joined_to_the_root(nodes, edges, root)


class TestOnlyAClaimThatStillStandsPins:
    async def test_a_retired_claim_does_not_hold_its_slot(self, session: AsyncSession) -> None:
        """Only ``status='active'`` rows attribute or name anywhere else in the
        system (LABEL_INTELLIGENCE.md §4). A withdrawn claim holding a permanent
        place in the picture is that rule broken in the one place a reader
        looks — and it spends budget the deeper hops need, since a pin is
        exempt from the quota."""
        repo, investigation_id = await new_investigation(session)
        level = await crowded_level(session, repo, investigation_id)
        label_id = await claim(session, level[BELOW_THE_CUT])
        assert level[BELOW_THE_CUT] in await read(repo, investigation_id), (
            "the claim must pin while it is active, or the retraction proves nothing"
        )

        await LabelRepository(session).set_status(label_id, "retired")
        returned = await read(repo, investigation_id)
        assert level[BELOW_THE_CUT] not in returned
        assert len(returned) == PER_LEVEL

    async def test_a_pending_claim_does_not_pin_either(self, session: AsyncSession) -> None:
        """Pending is a claim nobody has corroborated yet. It does not name and
        does not attribute, so it does not get to spend the picture's budget."""
        repo, investigation_id = await new_investigation(session)
        level = await crowded_level(session, repo, investigation_id)
        await LabelRepository(session).upsert_claim(
            chain=CHAIN,
            address=level[BELOW_THE_CUT],
            entity="Acme Exchange",
            category="vasp",
            role="deposit",
            confidence=0.9,
            status="pending",
            method="community",
            source="chainabuse",
            retrieved_at=HARVESTED,
        )

        assert level[BELOW_THE_CUT] not in await read(repo, investigation_id)


class TestTheNumbersTheShippedUiActuallyAsksFor:
    """The endpoint's defaults are not the numbers this graph is ever read at.

    Every other test here runs at ``GRAPH_NODE_LIMIT``/``GRAPH_PER_LEVEL`` — 240
    and 20 — because those are what an unparameterised caller gets. The only
    caller in the product overrides both: ``static/index.html`` polls
    ``/graph?limit=900&per_level=90``. Both exemptions added here are unbounded,
    so the budget they compete for is the one the reader is actually spending,
    and a quota four and a half times wider changes which nodes win their slots
    on merit and therefore which ancestors have to be carried in by hand.

    The two constants are read out of the page rather than restated, so this
    fails loudly on the day either side moves instead of quietly testing numbers
    nothing sends.
    """

    async def test_no_pin_is_orphaned_at_the_limit_the_page_polls_with(
        self, session: AsyncSession
    ) -> None:
        """The trace has to be bigger than what the page asks for, or the limit
        never binds and the ordering is untested: 1200 nodes against a limit of
        900. Every pin sits at hop 3 behind two unlabelled ancestors, and the
        pins are hung off the SMALLEST hop-2 chains so no ancestor of a pin wins
        a slot on its own value share."""
        limit, per_level = ui_graph_request()
        repo, investigation_id = await new_investigation(session)
        root = await root_node(session, repo, investigation_id)
        size = 400
        hop1 = await traced_level(
            session, repo, investigation_id, parent_id=root, hop_distance=1, size=size
        )
        hop2 = [
            (
                f"0xhop2_{index:03d}",
                await traced_node(
                    session,
                    repo,
                    investigation_id,
                    f"0xhop2_{index:03d}",
                    parent_id=hop1[index][1],
                    hop_distance=2,
                    value_share=(size - index) * 1_000,
                ),
            )
            for index in range(size)
        ]
        pins = []
        for index in range(size):
            address = f"0xpin_{index:03d}"
            pins.append(address)
            await traced_node(
                session,
                repo,
                investigation_id,
                address,
                parent_id=hop2[size - 1 - index][1],
                hop_distance=3,
                value_share=(size - index) * 1_000,
            )
            await claim(session, address)
        # And a dense labelled cluster at hop 1 as well, because a real trace
        # carries both shapes at once: the cluster is what starves the deeper
        # hops, the deep pins are what drag ancestors in past the cut.
        for address, _ in hop1[:350]:
            await claim(session, address)

        nodes, edges = await drawn(repo, investigation_id, limit=limit, per_level=per_level)
        returned = {node.address for node in nodes}
        reachable = joined_to_the_root(nodes, edges, root)
        levels = {1: [a for a, _ in hop1], 2: [a for a, _ in hop2], 3: pins}
        per_hop = {h: len([a for a in lvl if a in returned]) for h, lvl in levels.items()}

        assert len(nodes) <= limit
        assert all(kept > 0 for kept in per_hop.values()), f"a hop was wiped: {per_hop}"
        # Nothing on screen may float: an address the reader cannot trace back to
        # the subject is a dot, whether it is a pin or an ancestor carried in for
        # one. Read off the drawn edges, since the node list alone cannot see it.
        assert returned - reachable == set(), (
            f"{len(returned - reachable)} node(s) drawn with no line to the root: "
            f"{sorted(returned - reachable)[:10]}"
        )
