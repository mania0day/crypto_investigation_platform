"""Losing one acquisition feed without losing the address.

Every multi-feed adapter has the same shape: an address page is assembled from
two or three provider calls, and each of them can come back
``AllProvidersFailed``. Written as one chain of ``await``s, the loss of any
single feed raises, kills the page, and takes the whole branch of the trace
with it — one dead feed costs the address, and beyond it everything the
address would have led to.

That is the wrong trade once the keyed provider quotas are spent, which is the
state this system is designed to keep working in: an exhausted quota should
SLOW a trace, not stop it. So the secondary feeds are attempted through
:func:`optional_feed`, and their failure costs their own rows and nothing else.

The helper lives here rather than in each adapter because the two rules that
make the degradation safe are precisely the ones that drift when this is
copy-pasted:

1. **Only ``AllProvidersFailed`` is caught.** It is the pool's way of saying
   "nobody can serve this right now", which is a fact about coverage. A
   malformed payload, a decode bug, a programming error — anything else — comes
   straight back out, because a bug that degrades quietly is a bug that ships.
2. **The gap is recorded before the ``None`` is returned.** A caller cannot get
   the shorter answer without also getting the record of why it is shorter.
   "Returned fewer rows" and "says which rows are missing" have to travel
   together or the second one is forgotten at the next refactor — and nothing
   downstream can detect a transfer that was never fetched.

Which feed is *primary* is deliberately not decided here. It is chain
knowledge: an EVM page with no ``txlist`` is not a page, and returning an empty
one would tell the engine "this address never transacted", which is the single
answer this system must never invent. Each adapter keeps its own primary feed
on a hard ``await`` and passes only its secondaries through this.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from cipherchain.chains.base import FeedGap
from cipherchain.core.errors import AllProvidersFailed
from cipherchain.core.models import Capability
from cipherchain.providers.base import ProviderRequest, ProviderResponse
from cipherchain.providers.pool import ProviderPool


async def optional_feed(
    pool: ProviderPool,
    chain: str,
    capability: Capability,
    params: Mapping[str, Any],
    gaps: list[FeedGap],
) -> ProviderResponse | None:
    """Fetch a SECONDARY feed, or record its loss and let the caller carry on.

    Returns ``None`` only when no provider could serve ``capability``, and in
    that case ``gaps`` has grown by exactly one entry naming it. Every other
    failure propagates.
    """
    try:
        return await pool.fetch(ProviderRequest(chain, capability, dict(params)))
    except AllProvidersFailed as failure:
        gaps.append(FeedGap(chain=chain, capability=capability, detail=str(failure)))
        return None


__all__ = ["optional_feed"]
