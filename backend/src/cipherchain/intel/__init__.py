"""Intel — acquiring and verifying labels (LABEL_INTELLIGENCE.md).

Attribution *reads* labels; this context *acquires and verifies* them, and
the two never mix. The one invariant the whole context serves: only claims
that survived the lifecycle may ever attribute, and every step of that
lifecycle is on the record.
"""

from cipherchain.intel.policy import IntelClaim, arrival_status, corroborates, entity_stem
from cipherchain.intel.service import IntelService, ReconcileResult

__all__ = [
    "IntelClaim",
    "IntelService",
    "ReconcileResult",
    "arrival_status",
    "corroborates",
    "entity_stem",
]
