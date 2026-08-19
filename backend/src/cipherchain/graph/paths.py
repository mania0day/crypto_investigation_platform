"""Path reconstruction over an investigation's edges — the evidence trail
behind "nearest VASP" findings (vision §1: path evidence)."""

from __future__ import annotations

import uuid
from collections import deque

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from cipherchain.storage.tables import EdgeRow, MovementRow, NodeRow, TransactionRow


async def path_tx_hashes(
    session: AsyncSession,
    investigation_id: uuid.UUID,
    from_node_id: int,
    to_node_id: int,
    *,
    max_refs: int = 10,
) -> tuple[str, ...]:
    """Transaction hashes along the shortest node path, empty if unlinked.

    Investigation subgraphs are bounded, so a BFS over the loaded edge set
    is exact and cheap (Postgres-only decision; no graph DB required).

    **A speculative node is a wall, not a corridor.** These hashes become
    ``ONCHAIN_FACT`` evidence, whose whole promise is that a reader can verify
    the value path themselves. A mixer crossing is an edge the engine SELECTED
    rather than witnessed, so a route through one is not a connected value path
    and a list of hashes spanning it would be a false claim in the one evidence
    kind that is supposed to be checkable — the reader follows the hashes, hits
    the pool, and finds the trail does not join up.

    A speculative node is still allowed as the DESTINATION: the engine words
    those findings to say the hashes do not form a connected path (see
    ``_vasp_finding``), so the caveat is carried rather than implied. What is
    refused is routing *through* one to reach something else.

    This is enforced here rather than left to call order. Today both callers
    resolve refs at discovery, before any speculative node is expanded, so the
    bad path is unreachable — but that is a property of when the function
    happens to be called, not of what it guarantees, and moving attribution
    later would silently reopen it.
    """
    rows = await session.execute(
        select(EdgeRow.src_node_id, EdgeRow.dst_node_id, TransactionRow.tx_hash)
        .join(MovementRow, EdgeRow.movement_id == MovementRow.id)
        .join(TransactionRow, MovementRow.transaction_id == TransactionRow.id)
        .where(EdgeRow.investigation_id == investigation_id)
    )
    adjacency: dict[int, list[tuple[int, str]]] = {}
    for src, dst, tx_hash in rows.all():
        adjacency.setdefault(src, []).append((dst, tx_hash))
        adjacency.setdefault(dst, []).append((src, tx_hash))

    if from_node_id == to_node_id:
        return ()

    speculative = {
        node_id
        for (node_id,) in (
            await session.execute(
                select(NodeRow.id).where(
                    NodeRow.investigation_id == investigation_id,
                    NodeRow.speculative.is_(True),
                )
            )
        ).all()
    }

    parents: dict[int, tuple[int, str]] = {}
    queue: deque[int] = deque([from_node_id])
    visited = {from_node_id}
    while queue:
        current = queue.popleft()
        for neighbor, tx_hash in adjacency.get(current, []):
            if neighbor in visited:
                continue
            visited.add(neighbor)
            parents[neighbor] = (current, tx_hash)
            if neighbor == to_node_id:
                hashes: list[str] = []
                node = neighbor
                while node != from_node_id:
                    parent, hop_hash = parents[node]
                    hashes.append(hop_hash)
                    node = parent
                hashes.reverse()
                return tuple(hashes[:max_refs])
            # Reached but never expanded: a guess can terminate a path, never
            # carry one onward.
            if neighbor in speculative:
                continue
            queue.append(neighbor)
    return ()
