"""The intel lifecycle's storage guarantees (LABEL_INTELLIGENCE.md §4).

The properties under test are the ones the no-false-positives contract rests
on: only active rows reach the attributor's load, a re-harvest can never move
a status, corroboration is a second row rather than a mutation, and the audit
trail reads forward from a cursor.
"""

from datetime import UTC, datetime

import pytest
from sqlalchemy import text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from cipherchain.storage.repositories import LabelRepository

T0 = datetime(2026, 8, 11, 12, 0, tzinfo=UTC)
T1 = datetime(2026, 8, 11, 13, 0, tzinfo=UTC)


async def add_claim(repo: LabelRepository, **overrides: object) -> tuple[int, str]:
    claim: dict[str, object] = {
        "chain": "ethereum",
        "address": "0xaaa",
        "entity": "Acme Exchange (operational address)",
        "category": "vasp",
        "role": "operational",
        "confidence": 0.9,
        "status": "active",
        "method": "signature",
        "source": "acme-por",
        "retrieved_at": T0,
    }
    claim.update(overrides)
    return await repo.upsert_claim(**claim)  # type: ignore[arg-type]


class TestUpsert:
    async def test_re_harvest_of_identical_claim_is_unchanged(self, session: AsyncSession) -> None:
        repo = LabelRepository(session)
        first_id, outcome = await add_claim(repo)
        assert outcome == "added"
        again_id, outcome = await add_claim(repo, retrieved_at=T1)
        assert (again_id, outcome) == (first_id, "unchanged")
        # ... but the re-confirmation time IS recorded.
        (claim,) = await repo.claims_for("ethereum", "0xaaa")
        assert claim.retrieved_at == T1

    async def test_a_changed_claim_reports_updated(self, session: AsyncSession) -> None:
        repo = LabelRepository(session)
        await add_claim(repo)
        _, outcome = await add_claim(repo, confidence=0.85)
        assert outcome == "updated"

    async def test_a_method_change_is_a_change(self, session: AsyncSession) -> None:
        """A source can lose (or gain) its signatures between harvests. The
        stored method must follow the verification actually performed, and the
        downgrade must be visible as 'updated' so the caller records an event —
        review found method frozen at first sight, outcome 'unchanged'."""
        repo = LabelRepository(session)
        await add_claim(repo, method="signature")
        _, outcome = await add_claim(repo, method="first_party_published")
        assert outcome == "updated"
        (claim,) = await repo.claims_for("ethereum", "0xaaa")
        assert claim.method == "first_party_published"

    async def test_upsert_never_moves_status(self, session: AsyncSession) -> None:
        """A re-harvest resurrecting a retired label OR activating a pending
        one would be a lifecycle transition with no event and no decision
        behind it. Both halves pinned — review proved by mutation that the
        pending half alone was unguarded."""
        repo = LabelRepository(session)
        label_id, _ = await add_claim(repo)
        await repo.set_status(label_id, "retired")
        await add_claim(repo, status="active", retrieved_at=T1)
        (claim,) = await repo.claims_for("ethereum", "0xaaa")
        assert claim.status == "retired"

        await add_claim(repo, address="0xpending", status="pending", method="community")
        await add_claim(repo, address="0xpending", status="active", retrieved_at=T1)
        (pending,) = await repo.claims_for("ethereum", "0xpending")
        assert pending.status == "pending"
        assert [label.address for label in await repo.active_labels()] == []

    async def test_corroboration_is_a_second_row_not_a_mutation(
        self, session: AsyncSession
    ) -> None:
        repo = LabelRepository(session)
        await add_claim(repo)
        _, outcome = await add_claim(
            repo, source="community", method="community", status="pending", confidence=0.4
        )
        assert outcome == "added"
        claims = await repo.claims_for("ethereum", "0xaaa")
        assert len(claims) == 2
        # Strongest first — the display order every consumer should want.
        assert [c.source for c in claims] == ["acme-por", "community"]

    async def test_set_status_records_what_corroborated(self, session: AsyncSession) -> None:
        repo = LabelRepository(session)
        label_id, _ = await add_claim(repo, status="pending", method="community")
        await repo.set_status(label_id, "active", corroborated_by="acme-por")
        (claim,) = await repo.claims_for("ethereum", "0xaaa")
        assert (claim.status, claim.corroborated_by) == ("active", "acme-por")


class TestActiveLoad:
    async def test_only_active_rows_reach_the_attributor_load(self, session: AsyncSession) -> None:
        """THE property the no-false-positives contract rests on."""
        repo = LabelRepository(session)
        await add_claim(repo, address="0xactive", status="active")
        await add_claim(repo, address="0xpending", status="pending", method="community")
        retired_id, _ = await add_claim(repo, address="0xretired", status="active")
        await repo.set_status(retired_id, "retired")
        assert [label.address for label in await repo.active_labels()] == ["0xactive"]


class TestConstraints:
    async def test_confidence_is_rejected_at_both_bounds(self, session: AsyncSession) -> None:
        """The DB holds the same line as LabelRecord/Evidence: strictly inside
        (0, 1). Certainty is never a claim, and a zero-confidence 'claim'
        claims nothing — both bounds pinned, or a loosened rewrite passes."""
        repo = LabelRepository(session)
        with pytest.raises(IntegrityError):
            await add_claim(repo, confidence=1.0)
        await session.rollback()
        with pytest.raises(IntegrityError):
            await add_claim(repo, confidence=0.0)

    async def test_junk_category_and_status_are_rejected(self, session: AsyncSession) -> None:
        repo = LabelRepository(session)
        with pytest.raises(IntegrityError):
            await add_claim(repo, category="hero")
        await session.rollback()
        with pytest.raises(IntegrityError):
            await add_claim(repo, status="maybe")


class TestEvents:
    async def test_events_read_forward_from_a_cursor(self, session: AsyncSession) -> None:
        repo = LabelRepository(session)
        label_id, _ = await add_claim(repo)
        first = await repo.add_event(
            label_id=label_id, kind="added", reason="signature verified", actor="acme-por"
        )
        second = await repo.add_event(
            label_id=label_id, kind="retired", reason="source retracted", actor="acme-por"
        )
        assert [e.id for e in await repo.events_after(0)] == [first, second]
        tail = await repo.events_after(first)
        assert [(e.id, e.kind) for e in tail] == [(second, "retired")]
        assert await repo.events_after(second) == []

    async def test_the_audit_trail_rejects_unknown_transitions(self, session: AsyncSession) -> None:
        repo = LabelRepository(session)
        label_id, _ = await add_claim(repo)
        with pytest.raises(IntegrityError):
            await repo.add_event(label_id=label_id, kind="vanished", reason="?", actor="test")

    async def test_deleting_a_label_cannot_delete_its_history(self, session: AsyncSession) -> None:
        """The FK carries no ondelete on purpose: labels retire, they are not
        deleted, and audit history must not be deletable as a side effect of
        anything. Exercised with a raw DELETE so a future CASCADE — which no
        insert-path test would ever notice — fails here."""
        repo = LabelRepository(session)
        label_id, _ = await add_claim(repo)
        await repo.add_event(label_id=label_id, kind="added", reason="test", actor="test")
        with pytest.raises(IntegrityError):
            await session.execute(text("DELETE FROM labels WHERE id = :id"), {"id": label_id})
