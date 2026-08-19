"""Regression tests for the review fixes that don't have a natural home
elsewhere: breaker escape (#9), forward ordering (#7), resume budget (#2),
the confidence-not-certainty guard, and the sweep max_delay guard."""

from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from cipherchain.analysis.heuristics.sweep import detect_sweeps
from cipherchain.core.models import (
    Address,
    Asset,
    AssetKind,
    Evidence,
    EvidenceKind,
    Movement,
    MovementKind,
    Provenance,
    TxRef,
)
from cipherchain.investigation.budgets import Budgets, BudgetTracker
from cipherchain.providers.breaker import CircuitBreaker, CircuitState
from cipherchain.storage.repositories import FactRepository

NOW = datetime(2026, 8, 7, 12, 0, tzinfo=UTC)
PROV = Provenance(provider="t", retrieved_at=NOW, payload_sha256="a" * 64)
ETH = Asset(chain="ethereum", kind=AssetKind.NATIVE, symbol="ETH", decimals=18)


class _Clock:
    def __init__(self) -> None:
        self.now = 0.0

    def __call__(self) -> float:
        return self.now


def test_breaker_half_open_probe_slot_is_not_leaked_forever() -> None:
    """#9: a HALF_OPEN probe that never records its result must not wedge the
    breaker open permanently — reset_timeout releases the stale slot."""
    clock = _Clock()
    breaker = CircuitBreaker(failure_threshold=1, reset_timeout=30.0, clock=clock)
    breaker.record_failure()  # -> OPEN
    clock.now = 31.0
    assert breaker.allow()  # probe admitted, _probe_inflight=True
    assert breaker.state is CircuitState.HALF_OPEN
    # the probe neither succeeds nor fails (e.g. a 429 that only penalizes the
    # bucket) — without the fix, every future allow() would return False
    assert not breaker.allow()  # still within reset window
    clock.now = 62.0
    assert breaker.allow()  # stale slot released, fresh probe admitted


def test_confidence_is_never_certainty_for_inference_or_claim() -> None:
    Evidence(kind=EvidenceKind.HEURISTIC_INFERENCE, summary="x", heuristic="h@1", confidence=0.99)
    with pytest.raises(ValueError, match="never certainty"):
        Evidence(
            kind=EvidenceKind.HEURISTIC_INFERENCE, summary="x", heuristic="h@1", confidence=1.0
        )
    with pytest.raises(ValueError, match="never certainty"):
        Evidence(kind=EvidenceKind.THIRD_PARTY_CLAIM, summary="x", source="s", confidence=1.0)


def test_sweep_rejects_nonpositive_max_delay() -> None:
    addr = Address("ethereum", "0xa")
    with pytest.raises(ValueError, match="max_delay"):
        detect_sweeps(addr, [], [], max_delay=timedelta(0))


def test_resume_seeds_prior_spend_not_a_fresh_budget() -> None:
    """#2: seed_spent carries forward api_calls so a resume cannot grant a
    fresh budget; the wall clock is intentionally per-run."""
    tracker = BudgetTracker(Budgets(api_calls=100), clock=_Clock())
    tracker.seed_spent({"api_calls": 95, "txs_normalized": 300})
    assert tracker.api_calls_spent == 95
    assert tracker.txs_normalized == 300
    tracker.charge_api(5)
    assert tracker.exhausted() == "api_calls"  # 100 reached, not reset to 0


async def test_forward_query_returns_earliest_after_cutoff(session: AsyncSession) -> None:
    """#7: forward expansion must return the immediate post-arrival hops, not
    the newest movements at a busy address."""
    facts = FactRepository(session)
    focus = "0xbusy"
    # value arrives, is swept next minute, then many later unrelated payouts
    hops = [
        ("0xsweep", NOW + timedelta(minutes=1), 1),
    ] + [(f"0xlater{i}", NOW + timedelta(days=i + 1), i + 2) for i in range(5)]
    for target, when, idx in hops:
        tx = TxRef(chain="ethereum", tx_hash=f"0x{idx:04x}", timestamp=when)
        await facts.store_movements(
            tx,
            [
                Movement(
                    tx=tx,
                    asset=ETH,
                    amount=100,
                    kind=MovementKind.NATIVE,
                    from_address=Address("ethereum", focus),
                    to_address=Address("ethereum", target),
                    index=0,
                    provenance=PROV,
                    dedup_key="native",
                )
            ],
        )
    focus_id = await facts.get_or_create_address(Address("ethereum", focus))
    outgoing = await facts.movements_from_address(focus_id, limit=2)
    # earliest-first: the sweep is the first result, never truncated away
    assert outgoing[0].tx_hash == "0x0001"
