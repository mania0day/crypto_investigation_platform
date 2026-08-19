"""Tron paging, and the false coverage claim it existed to cause.

`next_cursor` was hardcoded `None`. The engine only calls
`mark_history_truncated` when a cursor comes back, so every Tron address was
read once and then recorded as FULLY READ — and `_coverage` would print "no
address was left partially read" for a trace that saw one page of a busy
exchange wallet. Tron carries more VASP labels than any other chain here
(17,803), so it was the worst chain to be wrong about.
"""

from typing import Any

import pytest

from cipherchain.chains.base import TimeWindow
from cipherchain.chains.tron.adapter import TronAdapter, _join_cursor, _split_cursor
from cipherchain.core.models import Address, Capability

NATIVE_FP = "NnnnFingerprintAAA"
TOKEN_FP = "TttFingerprintBBB"


def body(rows: list[dict[str, Any]], fingerprint: str | None, more: bool) -> dict[str, Any]:
    meta: dict[str, Any] = {"at": 1}
    if fingerprint:
        meta["fingerprint"] = fingerprint
    if more:
        meta["links"] = {"next": "https://api.trongrid.io/next"}
    return {"success": True, "data": rows, "meta": meta}


class TestCursor:
    def test_no_more_pages_on_either_feed_means_no_cursor(self) -> None:
        assert _join_cursor(body([], NATIVE_FP, False), body([], TOKEN_FP, False)) is None

    def test_a_final_page_carrying_a_fingerprint_still_stops(self) -> None:
        """TronGrid returns a fingerprint on the LAST page too. Keying on the
        fingerprint alone pages forever over the same rows, so the signal is
        `meta.links.next`."""
        assert _join_cursor(body([], NATIVE_FP, False), body([], TOKEN_FP, False)) is None

    def test_both_feeds_continuing_carries_both_fingerprints(self) -> None:
        cursor = _join_cursor(body([], NATIVE_FP, True), body([], TOKEN_FP, True))
        assert cursor == f"{NATIVE_FP}|{TOKEN_FP}"
        assert _split_cursor(cursor) == (NATIVE_FP, TOKEN_FP)

    def test_one_exhausted_feed_leaves_its_half_empty(self) -> None:
        """Advancing only the live feed matters: reusing a spent fingerprint
        would re-read the other feed from the top and drop everything between."""
        cursor = _join_cursor(body([], NATIVE_FP, True), body([], TOKEN_FP, False))
        assert cursor == f"{NATIVE_FP}|"
        assert _split_cursor(cursor) == (NATIVE_FP, None)

    def test_an_absent_cursor_asks_both_feeds_from_the_top(self) -> None:
        assert _split_cursor(None) == (None, None)
        assert _split_cursor("") == (None, None)


class FakePool:
    """Records what the adapter asked for, and serves canned envelopes."""

    def __init__(self, native: dict[str, Any], token: dict[str, Any]) -> None:
        self._native = native
        self._token = token
        self.requests: list[tuple[str, dict[str, Any]]] = []

    async def fetch(self, request: Any) -> Any:
        self.requests.append((request.capability.value, dict(request.params)))
        payload = self._native if request.capability is Capability.ADDRESS_HISTORY else self._token

        class _Response:
            def __init__(self, data: dict[str, Any]) -> None:
                self.payload = data

            def provenance(self) -> Any:
                from datetime import UTC, datetime

                from cipherchain.core.models import Provenance

                return Provenance("trongrid", datetime.now(UTC), "a" * 64)

        return _Response(payload)


ROW = {
    "txID": "abc123",
    "block_timestamp": 1_700_000_000_000,
    "raw_data": {"contract": []},
    "ret": [{"contractRet": "SUCCESS"}],
}


class TestAdapterPaging:
    async def test_a_full_page_reports_more_history_to_come(self) -> None:
        pool = FakePool(body([ROW], NATIVE_FP, True), body([], TOKEN_FP, False))
        adapter = TronAdapter(pool)  # type: ignore[arg-type]
        page = await adapter.address_history(Address("tron", "T" + "a" * 33), limit=20)
        assert page.next_cursor == f"{NATIVE_FP}|"

    async def test_the_cursor_is_sent_back_to_the_right_feed(self) -> None:
        pool = FakePool(body([ROW], NATIVE_FP, True), body([], TOKEN_FP, True))
        adapter = TronAdapter(pool)  # type: ignore[arg-type]
        await adapter.address_history(
            Address("tron", "T" + "a" * 33), limit=20, cursor=f"{NATIVE_FP}|{TOKEN_FP}"
        )
        sent = dict(pool.requests)
        assert sent["address_history"]["fingerprint"] == NATIVE_FP
        assert sent["token_transfers"]["fingerprint"] == TOKEN_FP

    async def test_the_first_page_asks_for_no_fingerprint(self) -> None:
        pool = FakePool(body([ROW], None, False), body([], None, False))
        adapter = TronAdapter(pool)  # type: ignore[arg-type]
        await adapter.address_history(Address("tron", "T" + "a" * 33), limit=20)
        assert all(params.get("fingerprint") is None for _, params in pool.requests)

    async def test_a_time_window_does_not_end_pagination(self) -> None:
        """The Bitcoin regression, guarded here too: a window that filters a
        page empty must not be read as 'no more history'."""
        pool = FakePool(body([ROW], NATIVE_FP, True), body([], TOKEN_FP, True))
        adapter = TronAdapter(pool)  # type: ignore[arg-type]
        from datetime import UTC, datetime

        page = await adapter.address_history(
            Address("tron", "T" + "a" * 33),
            limit=20,
            window=TimeWindow(datetime(2030, 1, 1, tzinfo=UTC), None),
        )
        assert page.items == ()
        assert page.next_cursor is not None, "an emptied page still has more behind it"


@pytest.mark.parametrize("cursor", ["", None])
async def test_empty_and_missing_cursors_behave_identically(cursor: str | None) -> None:
    assert _split_cursor(cursor) == (None, None)
