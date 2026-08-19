"""Address clustering — deciding which addresses share one controller.

Clustering is how one sourced label becomes many. A single verified Binance
address is worth one attribution; the *cluster* it belongs to may be worth
thousands, and coverage is the reason most traces fail to name an endpoint
(REACHING_THE_VASP.md §6).

The techniques here are deterministic, not statistical. A cluster claim must
be defensible as a statement about how the ledger works — "spending these
inputs required every one of their keys" — never as a resemblance. Anything
weaker belongs in `analysis.heuristics`, which emits inferences rather than
identity.
"""

from cipherchain.analysis.clustering.cospend import (
    COSPEND_HEURISTIC,
    ClusterProposal,
    SpendingTransaction,
    build_clusters,
    looks_like_coinjoin,
    propose_cluster_labels,
)

__all__ = [
    "COSPEND_HEURISTIC",
    "ClusterProposal",
    "SpendingTransaction",
    "build_clusters",
    "looks_like_coinjoin",
    "propose_cluster_labels",
]
