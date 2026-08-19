"""The intel service against real storage: every transition leaves its event.

The through-line: a community report must be able to BECOME evidence, but
only by corroboration, corroboration must KEEP holding, and the whole journey
must be reconstructable from label_events alone. The attack tests reproduce
scenarios adversarial review demonstrated end-to-end against the previous
version of this layer — each one used to succeed.
"""

from datetime import UTC, datetime

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from cipherchain.intel.policy import IntelClaim
from cipherchain.intel.service import CORROBORATOR, IntelService
from cipherchain.storage.repositories import LabelRepository

T0 = datetime(2026, 8, 11, 12, 0, tzinfo=UTC)


def claim(**overrides: object) -> IntelClaim:
    base: dict[str, object] = {
        "chain": "ethereum",
        "address": "0xAAA",  # mixed case on purpose: ingest must normalize
        "entity": "Acme Exchange",
        "category": "vasp",
        "role": "operational",
        "confidence": 0.9,
        "method": "signature",
        "source": "acme-por",
        "retrieved_at": T0,
    }
    base.update(overrides)
    return IntelClaim(**base)  # type: ignore[arg-type]


def report(**overrides: object) -> IntelClaim:
    base: dict[str, object] = {
        "method": "community",
        "source": "report:key-1",
        "entity": "Acme",
        "confidence": 0.4,
        "reporter": "key-1",
    }
    base.update(overrides)
    return claim(**base)


class TestIngest:
    async def test_a_trusted_claim_arrives_active_and_evented(self, session: AsyncSession) -> None:
        service = IntelService(session)
        assert await service.ingest(claim()) == "added"
        repo = LabelRepository(session)
        (label,) = await repo.active_labels()
        assert (label.status, label.address) == ("active", "0xaaa")  # normalized
        (event,) = await repo.events_after(0)
        assert (event.kind, event.actor) == ("added", "acme-por")

    async def test_a_community_report_arrives_pending(self, session: AsyncSession) -> None:
        service = IntelService(session)
        await service.ingest(report())
        repo = LabelRepository(session)
        assert await repo.active_labels() == []
        (pending,) = await repo.pending_labels()
        assert pending.reporter == "key-1"
        (event,) = await repo.events_after(0)
        assert "pending corroboration" in event.reason

    async def test_an_unchanged_reharvest_is_not_an_event(self, session: AsyncSession) -> None:
        """retrieved_at records the re-confirmation; an audit trail that logs
        every quiet re-read drowns the transitions it exists to show."""
        service = IntelService(session)
        await service.ingest(claim())
        assert await service.ingest(claim(retrieved_at=datetime(2026, 8, 12, tzinfo=UTC))) == (
            "unchanged"
        )
        repo = LabelRepository(session)
        assert len(await repo.events_after(0)) == 1  # the original 'added' only

    async def test_a_changed_claim_events_updated(self, session: AsyncSession) -> None:
        service = IntelService(session)
        await service.ingest(claim())
        await service.ingest(claim(entity="Acme Exchange 2"))
        repo = LabelRepository(session)
        events = await repo.events_after(0)
        assert [e.kind for e in events] == ["added", "updated"]


class TestResettle:
    """An updated claim is a different claim: status re-derives immediately.
    The gap between edit and next cycle is the window the post-promotion
    mutation attack lived in."""

    async def test_a_method_downgrade_demotes_immediately(self, session: AsyncSession) -> None:
        """Review confirmed: signature claim re-filed as community stayed
        active — an unverified claim in the attributor's load."""
        service = IntelService(session)
        await service.ingest(claim())
        await service.ingest(claim(method="community"))
        repo = LabelRepository(session)
        assert await repo.active_labels() == []
        (row,) = await repo.pending_labels()
        assert row.method == "community"
        assert [e.kind for e in await repo.events_after(0)] == ["added", "updated", "demoted"]

    async def test_a_method_upgrade_activates_immediately(self, session: AsyncSession) -> None:
        service = IntelService(session)
        await service.ingest(claim(method="licensed_dataset", confidence=0.75))
        # Same source later publishes signed proofs.
        await service.ingest(claim(method="signature", confidence=0.75))
        repo = LabelRepository(session)
        (label,) = await repo.active_labels()
        assert label.method == "signature"
        # Already active: an in-tier method change is an update, not a transition.
        assert [e.kind for e in await repo.events_after(0)] == ["added", "updated"]

    async def test_retired_rows_never_resettle(self, session: AsyncSession) -> None:
        service = IntelService(session)
        await service.ingest(claim())
        repo = LabelRepository(session)
        (label,) = await repo.active_labels()
        await service.retire(label, reason="absent from refresh", actor="acme-por")
        await service.ingest(claim(entity="Acme Reborn"))
        (row,) = await repo.claims_for("ethereum", "0xaaa")
        assert row.status == "retired"

    async def test_the_post_promotion_mutation_attack_is_dead(self, session: AsyncSession) -> None:
        """THE attack review reproduced end-to-end: promote honest content,
        then edit the row into 'Lazarus Group'/sanctioned. It stayed active
        with the original corroborated_by — attacker-chosen content, forged
        provenance, in the attributor's load. Now: instant demotion, citation
        cleared, and reconcile refuses to re-promote what nothing supports."""
        service = IntelService(session)
        await service.ingest(claim(entity="Binance 14", source="etherscan-tags"))
        await service.ingest(report(entity="Binance"))
        assert (await service.reconcile()).promoted != []

        await service.ingest(report(entity="Lazarus Group", category="sanctioned"))
        repo = LabelRepository(session)
        mutated = next(
            c for c in await repo.claims_for("ethereum", "0xaaa") if c.source == "report:key-1"
        )
        assert (mutated.status, mutated.corroborated_by) == ("pending", None)
        result = await service.reconcile()
        assert result.promoted == []  # nothing corroborates the new content
        active_entities = {c.entity for c in await repo.active_labels()}
        assert "Lazarus Group" not in active_entities


class TestReconcile:
    async def test_the_full_journey_of_a_community_report(self, session: AsyncSession) -> None:
        """Report arrives pending → independent source lands → bot promotes.
        The journey must be reconstructable from events alone."""
        service = IntelService(session)
        await service.ingest(report())
        assert (await service.reconcile()).promoted == []  # nothing agrees yet

        await service.ingest(claim(entity="Acme 7", source="acme-por"))
        (promoted_id,) = (await service.reconcile()).promoted

        repo = LabelRepository(session)
        promoted = next(
            c for c in await repo.claims_for("ethereum", "0xaaa") if c.id == promoted_id
        )
        assert (promoted.status, promoted.corroborated_by) == ("active", "acme-por")
        kinds = [(e.kind, e.actor) for e in await repo.events_after(0)]
        assert ("promoted", CORROBORATOR) in kinds

    async def test_promotion_does_not_depend_on_arrival_order(self, session: AsyncSession) -> None:
        service = IntelService(session)
        await service.ingest(claim(entity="Acme 7", source="acme-por"))
        await service.ingest(report())
        assert len((await service.reconcile()).promoted) == 1

    async def test_two_reports_cannot_promote_each_other(self, session: AsyncSession) -> None:
        service = IntelService(session)
        await service.ingest(report(source="report:key-a", reporter="key-a"))
        await service.ingest(report(source="report:key-b", reporter="key-b"))
        assert (await service.reconcile()).promoted == []

    async def test_a_promoted_report_cannot_corroborate_another(
        self, session: AsyncSession
    ) -> None:
        """Trust flows one way. Review confirmed the transitive version: two
        reports that could never promote each other directly did so across
        cycles once one had been promoted — and held each other active after
        the real source retired."""
        service = IntelService(session)
        await service.ingest(claim(entity="Acme 7", source="acme-por"))
        await service.ingest(report(source="report:key-a", reporter="key-a"))
        assert (await service.reconcile()).promoted != []

        repo = LabelRepository(session)
        (trusted,) = [c for c in await repo.active_labels() if c.source == "acme-por"]
        await service.retire(trusted, reason="source retracted", actor="acme-por")

        await service.ingest(report(source="report:key-b", reporter="key-b"))
        result = await service.reconcile()
        # key-a loses its basis; key-b never gains one.
        assert result.promoted == []
        assert await repo.active_labels() == []

    async def test_retiring_the_corroborator_demotes_the_echo(self, session: AsyncSession) -> None:
        """Review confirmed: the promoted echo outlived the only evidence
        that ever supported it, citing a retired source forever."""
        service = IntelService(session)
        await service.ingest(claim(entity="Acme 7", source="acme-por"))
        await service.ingest(report())
        assert (await service.reconcile()).promoted != []

        repo = LabelRepository(session)
        (trusted,) = [c for c in await repo.active_labels() if c.source == "acme-por"]
        await service.retire(trusted, reason="absent from acme-por refresh", actor="acme-por")

        result = await service.reconcile()
        assert len(result.demoted) == 1
        assert await repo.active_labels() == []
        (echo,) = await repo.pending_labels()
        assert echo.corroborated_by is None, "a dangling citation is worse than none"
        demote_events = [e for e in await repo.events_after(0) if e.kind == "demoted"]
        assert "acme-por" in demote_events[0].reason  # the audit says what was lost

    async def test_evidence_about_one_address_says_nothing_about_another(
        self, session: AsyncSession
    ) -> None:
        """Mutation testing proved the suite could not pin the address join —
        a bot iterating ALL active labels instead of the claim's address
        passed every test."""
        service = IntelService(session)
        await service.ingest(claim(entity="Acme 7", source="acme-por", address="0xaaa"))
        await service.ingest(report(address="0xbbb"))
        assert (await service.reconcile()).promoted == []

    async def test_a_disagreeing_entity_does_not_promote(self, session: AsyncSession) -> None:
        service = IntelService(session)
        await service.ingest(report(entity="Zenith Exchange"))
        await service.ingest(claim(entity="Acme 7", source="acme-por"))
        assert (await service.reconcile()).promoted == []
        repo = LabelRepository(session)
        assert await repo.pending_labels() != []


class TestTransactionality:
    async def test_a_transition_and_its_event_are_one_transaction(
        self, session: AsyncSession, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Mutation testing proved the old suite only checked the event
        EXISTS, not that it is atomic with the row change — a commit inserted
        between the two passed every test. Fault-inject the event write and
        prove the whole promotion rolls back."""
        service = IntelService(session)
        await service.ingest(claim(entity="Acme 7", source="acme-por"))
        await service.ingest(report())
        # Commit the setup so the rollback below isolates exactly the failed
        # promotion (test isolation is TRUNCATE-per-test, not rollback).
        await session.commit()

        async def boom(self: object, **kwargs: object) -> int:
            raise RuntimeError("event write failed")

        monkeypatch.setattr(LabelRepository, "add_event", boom)
        with pytest.raises(RuntimeError):
            await service.reconcile()
        monkeypatch.undo()
        await session.rollback()

        repo = LabelRepository(session)
        (echo,) = await repo.pending_labels()
        assert echo.status == "pending", "a promotion its event cannot show must not survive"
        assert all(e.kind != "promoted" for e in await repo.events_after(0))


class TestRetire:
    async def test_retirement_is_evented_and_final_for_the_load(
        self, session: AsyncSession
    ) -> None:
        service = IntelService(session)
        await service.ingest(claim())
        repo = LabelRepository(session)
        (label,) = await repo.active_labels()
        await service.retire(label, reason="absent from acme-por refresh", actor="acme-por")
        assert await repo.active_labels() == []
        events = await repo.events_after(0)
        assert [e.kind for e in events] == ["added", "retired"]
