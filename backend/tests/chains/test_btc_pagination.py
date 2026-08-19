"""A time window must not end pagination early.

The cursor used to be derived from the list that survived the confirmed- and
window-filters, so a full provider page emptied by a window looked like the
last page. A windowed trace then reported "exhausted" for transactions it had
never fetched. This matters more now that `next_cursor` also decides whether a
finding declares its history truncated (Ruling 2/4).
"""

from datetime import UTC, datetime

from cipherchain.chains.base import TimeWindow
from cipherchain.chains.bitcoin import BitcoinAdapter
from cipherchain.core.models import Address
from cipherchain.providers.base import ProviderResponse

BASE = datetime(2026, 6, 1, tzinfo=UTC)
PAGE_SIZE = 25


class FullPagePool:
    """Serves a FULL esplora page every time, like a busy address would."""

    async def fetch(self, request: object) -> ProviderResponse:
        stamp = int(BASE.timestamp())
        payload = [
            {
                "txid": f"tx{i:02d}",
                "status": {"confirmed": True, "block_time": stamp + i * 3600},
                "vin": [],
                "vout": [],
            }
            for i in range(PAGE_SIZE)
        ]
        return ProviderResponse(
            provider="fake",
            payload=payload,
            raw=b"[]",
            retrieved_at=datetime.now(tz=UTC),
            payload_sha256="0" * 64,
        )


ADDRESS = Address("bitcoin", "bc1qexample")


async def test_full_page_pages_on_without_a_window() -> None:
    page = await BitcoinAdapter(FullPagePool()).address_history(ADDRESS, limit=PAGE_SIZE)
    assert len(page.items) == PAGE_SIZE
    assert page.next_cursor == "tx24"


async def test_narrow_window_does_not_end_pagination() -> None:
    """The regression: most of a full page discarded, more pages still exist."""
    window = TimeWindow(start=BASE.replace(hour=20), end=None)
    page = await BitcoinAdapter(FullPagePool()).address_history(
        ADDRESS, window=window, limit=PAGE_SIZE
    )
    assert len(page.items) < PAGE_SIZE, "the window should discard most of the page"
    assert page.next_cursor == "tx24", (
        "a full provider page must keep paging even when the window empties it, "
        "and the cursor must advance past the whole page"
    )


async def test_window_that_empties_a_page_still_pages_on() -> None:
    window = TimeWindow(start=datetime(2030, 1, 1, tzinfo=UTC), end=None)
    page = await BitcoinAdapter(FullPagePool()).address_history(
        ADDRESS, window=window, limit=PAGE_SIZE
    )
    assert page.items == ()
    assert page.next_cursor == "tx24"
