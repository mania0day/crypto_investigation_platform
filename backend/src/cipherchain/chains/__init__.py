"""Chain SDK — every blockchain behind one frozen contract.

The contract lives in ``base`` and was frozen 2026-08-07
(docs/research/CHAIN_SDK_INTERFACE.md). Adapters are completely independent
of one another; shared behavior belongs here, never in a sibling adapter.
"""

from cipherchain.chains.base import (
    BridgeDirection,
    BridgeHint,
    ChainAdapter,
    ChainRegistry,
    ChainTransaction,
    FeedGap,
    HistoryPage,
    NormalizedTransaction,
    TimeWindow,
    feed_name_for_code,
)

__all__ = [
    "BridgeDirection",
    "BridgeHint",
    "ChainAdapter",
    "ChainRegistry",
    "ChainTransaction",
    # Exported beside HistoryPage because it is part of that page's shape: a
    # caller that reads `page.gaps` needs to be able to name what it got, and
    # `feed_name_for_code` is how a stored gap code becomes a sentence again.
    "FeedGap",
    "HistoryPage",
    "NormalizedTransaction",
    "TimeWindow",
    "feed_name_for_code",
]
