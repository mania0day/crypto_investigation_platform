"""Co-spend clustering: the multi-input heuristic, with the guards it needs.

If one transaction spends inputs belonging to several addresses, whoever
built it held the private key of every one of them. That is not a
resemblance or a pattern — it is what signing a transaction requires. It is
the strongest same-entity signal available on a UTXO chain and the
foundation of every commercial clustering product.

It has exactly one catastrophic failure mode, and this module exists mostly
to guard it.

CoinJoin
--------
A CoinJoin is a transaction deliberately built from inputs owned by
*different* people. Clustering one merges strangers into a single entity, and
because the merge is transitive it does not stay local: one bad transaction
can weld two unrelated clusters together and every label on either side then
names the wrong party. In a tool whose output is a subpoena target, that is
the worst outcome in this repository.

Two guards, both deliberately over-eager, because refusing to cluster costs
coverage while wrongly clustering costs correctness:

1. **Equal-output structure.** CoinJoin implementations (Wasabi, Whirlpool,
   JoinMarket) pay several participants the *same* amount, which is what
   makes the outputs unlinkable. Several inputs plus repeated identical
   output values is the signature.
2. **Known mixer addresses.** A transaction touching an address the label
   store calls a mixer is never clustered, whatever its shape.

Both guards fail toward refusal. A normal transaction that happens to pay two
people the same round amount is skipped, and the only cost is one missed
cluster edge.

Payjoin remains a known gap — it is designed to look like an ordinary
two-party spend and is documented in REACHING_THE_VASP.md rather than
silently handled here.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field

COSPEND_HEURISTIC = "cluster-cospend@1"

# A CoinJoin needs enough participants to be worth doing. Two-input
# transactions are overwhelmingly ordinary spends, and excluding them would
# discard most real clustering signal for almost no safety gain.
DEFAULT_MIN_COINJOIN_INPUTS = 3

# How many outputs must share one value before the shape reads as deliberate.
DEFAULT_MIN_EQUAL_OUTPUTS = 2


@dataclass(frozen=True, slots=True)
class SpendingTransaction:
    """One transaction's clustering-relevant shape.

    Deliberately not a database row: clustering is pure and testable without
    a session, and the caller decides how to read the fact store.
    """

    tx_hash: str
    input_address_ids: tuple[int, ...]
    output_amounts: tuple[int, ...] = ()

    @property
    def distinct_inputs(self) -> frozenset[int]:
        return frozenset(self.input_address_ids)


@dataclass(frozen=True, slots=True)
class ClusterProposal:
    """An entity name, and the addresses co-spending says share its owner."""

    entity: str
    category: str
    seed_address_ids: frozenset[int]
    new_address_ids: frozenset[int]
    source: str
    confidence: float
    cluster_size: int

    def __post_init__(self) -> None:
        if not self.entity:
            raise ValueError("a cluster proposal must name an entity")
        if not self.seed_address_ids:
            raise ValueError("a cluster proposal must rest on at least one seed label")
        if not 0.0 < self.confidence < 1.0:
            # Same rule as every other claim in the system: a cluster
            # membership is inferred from a label that was itself a claim,
            # so it can never be more certain than one.
            raise ValueError("cluster confidence must be strictly inside (0, 1)")


@dataclass(frozen=True, slots=True)
class ClusteringReport:
    """What clustering did, and — more usefully — what it refused to do."""

    clusters: tuple[frozenset[int], ...] = ()
    coinjoins_skipped: int = 0
    mixer_transactions_skipped: int = 0
    conflicted_clusters: tuple[tuple[str, ...], ...] = field(default=())

    @property
    def clustered_addresses(self) -> int:
        return sum(len(c) for c in self.clusters)


def largest_equal_output_group(amounts: Sequence[int]) -> int:
    """Size of the biggest set of outputs sharing one exact value."""
    counts: dict[int, int] = {}
    for amount in amounts:
        counts[amount] = counts.get(amount, 0) + 1
    return max(counts.values(), default=0)


def looks_like_coinjoin(
    tx: SpendingTransaction,
    *,
    min_inputs: int = DEFAULT_MIN_COINJOIN_INPUTS,
    min_equal_outputs: int = DEFAULT_MIN_EQUAL_OUTPUTS,
) -> bool:
    """Does this transaction's shape suggest inputs from several owners?

    Over-eager on purpose — see the module docstring. A false positive here
    costs one cluster edge; a false negative welds two strangers together.
    """
    if len(tx.distinct_inputs) < min_inputs:
        return False
    return largest_equal_output_group(tx.output_amounts) >= min_equal_outputs


class _UnionFind:
    """Disjoint sets over address ids, with path compression."""

    def __init__(self) -> None:
        self._parent: dict[int, int] = {}

    def find(self, item: int) -> int:
        parent = self._parent.setdefault(item, item)
        while parent != item:
            item, parent = parent, self._parent.setdefault(parent, parent)
            self._parent[item] = parent
        return item

    def union(self, left: int, right: int) -> None:
        left_root, right_root = self.find(left), self.find(right)
        if left_root != right_root:
            self._parent[right_root] = left_root

    def groups(self) -> list[frozenset[int]]:
        buckets: dict[int, set[int]] = {}
        for item in self._parent:
            buckets.setdefault(self.find(item), set()).add(item)
        return [frozenset(members) for members in buckets.values() if len(members) > 1]


def build_clusters(
    transactions: Iterable[SpendingTransaction],
    *,
    mixer_address_ids: frozenset[int] = frozenset(),
    min_coinjoin_inputs: int = DEFAULT_MIN_COINJOIN_INPUTS,
) -> ClusteringReport:
    """Group addresses that provably share a controller.

    Only transactions with two or more distinct spending addresses carry any
    signal; single-input transactions are ignored rather than forming
    one-address clusters, which would be noise.
    """
    union = _UnionFind()
    coinjoins = 0
    mixer_touched = 0

    for tx in transactions:
        spenders = sorted(tx.distinct_inputs)
        if len(spenders) < 2:
            continue
        if mixer_address_ids and not mixer_address_ids.isdisjoint(spenders):
            # A mixer's own inputs belong to its participants, not to it.
            mixer_touched += 1
            continue
        if looks_like_coinjoin(tx, min_inputs=min_coinjoin_inputs):
            coinjoins += 1
            continue
        first = spenders[0]
        for other in spenders[1:]:
            union.union(first, other)

    return ClusteringReport(
        clusters=tuple(union.groups()),
        coinjoins_skipped=coinjoins,
        mixer_transactions_skipped=mixer_touched,
    )


def propose_cluster_labels(
    report: ClusteringReport,
    seeds: Mapping[int, tuple[str, str, str, float]],
    *,
    confidence_factor: float = 0.9,
) -> tuple[list[ClusterProposal], list[tuple[str, ...]]]:
    """Turn clusters into label proposals for their unlabelled members.

    ``seeds`` maps an address id to ``(entity, category, source, confidence)``
    taken from an already-active label.

    Returns ``(proposals, conflicts)``.

    A cluster containing labels for **two different entities** is a
    contradiction, not a tie to be broken: co-spending says one controller,
    so two exchanges inside one cluster means the clustering is wrong — a
    CoinJoin slipped a guard, or a source is mislabelled. Such a cluster is
    refused whole and reported. Picking a winner would publish a name derived
    from a cluster we have just proved unsound.
    """
    proposals: list[ClusterProposal] = []
    conflicts: list[tuple[str, ...]] = []

    for cluster in report.clusters:
        labelled = {address: seeds[address] for address in cluster if address in seeds}
        if not labelled:
            continue

        entities = {entity for entity, _, _, _ in labelled.values()}
        if len(entities) > 1:
            conflicts.append(tuple(sorted(entities)))
            continue

        entity, category, source, seed_confidence = next(iter(labelled.values()))
        new_addresses = frozenset(cluster) - set(labelled)
        if not new_addresses:
            continue

        # Never above the seed: a derived claim cannot outrank the claim it
        # was derived from.
        confidence = min(seed_confidence, seed_confidence * confidence_factor)
        proposals.append(
            ClusterProposal(
                entity=entity,
                category=category,
                seed_address_ids=frozenset(labelled),
                new_address_ids=new_addresses,
                source=source,
                confidence=confidence,
                cluster_size=len(cluster),
            )
        )

    return proposals, conflicts
