"""GraphStore port — traversal queries over stored investigation data.

Postgres-backed per the approved storage decision; a dedicated graph
database would slot in behind this package if a query pattern ever
demands it.
"""

from cipherchain.graph.paths import path_tx_hashes

__all__ = ["path_tx_hashes"]
