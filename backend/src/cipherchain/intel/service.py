"""The intel service: policy applied over storage, every decision an event.

Two invariants this layer owns:

- **Every lifecycle transition writes its event in the same transaction as
  the row change.** A promotion the events table cannot show is a promotion
  nobody can audit — the service never commits between the two (pinned by a
  fault-injection test).
- **Active status is continuously justified, never stamped.** Adversarial
  review proved the stamp version attackable: promote honest content, then
  edit the row into something nobody corroborated; or retire the one source
  a promotion rested on and the echo survives it. So an untrusted claim's
  activation is re-earned — synchronously when its own content changes, and
  every reconcile cycle against the sources that still stand.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field

from sqlalchemy.ext.asyncio import AsyncSession

from cipherchain.analysis.attribution.labels import normalize_address
from cipherchain.intel.policy import IntelClaim, arrival_status, corroborates
from cipherchain.storage.repositories import LabelRepository, StoredLabel

logger = logging.getLogger(__name__)

# Actor recorded on automated promotions/demotions. Not a source name on
# purpose: the bot holds no claim of its own, it only recognizes whether
# agreement between sources that do still stands.
CORROBORATOR = "corroboration-bot"


@dataclass(frozen=True, slots=True)
class ReconcileResult:
    promoted: list[int] = field(default_factory=list)
    demoted: list[int] = field(default_factory=list)


class IntelService:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session
        self._labels = LabelRepository(session)

    async def ingest(self, claim: IntelClaim) -> str:
        """One claim in, status decided by policy, outcome evented. Returns
        the repository outcome (``added`` | ``updated`` | ``unchanged``).

        An unchanged re-harvest is deliberately not an event: the row's
        ``retrieved_at`` records the re-confirmation, and an audit trail that
        logs every quiet re-read drowns the transitions it exists to show.

        An UPDATED claim is a different claim, so its status is re-derived
        from policy immediately — not left for the next cycle, because the
        gap between edit and cycle is exactly the window the post-promotion
        mutation attack lived in. Retired rows are the one exception: nothing
        implicit leads out of retired.
        """
        status = arrival_status(claim.method)
        label_id, outcome = await self._labels.upsert_claim(
            chain=claim.chain,
            address=normalize_address(claim.address),
            entity=claim.entity,
            category=claim.category,
            role=claim.role,
            confidence=claim.confidence,
            status=status,
            method=claim.method,
            source=claim.source,
            retrieved_at=claim.retrieved_at,
            source_date=claim.source_date,
            evidence_url=claim.evidence_url,
            reporter=claim.reporter,
        )
        if outcome == "added":
            await self._labels.add_event(
                label_id=label_id,
                kind="added",
                reason=f"{claim.method} claim: {claim.entity}"
                + ("" if status == "active" else " (pending corroboration)"),
                actor=claim.source,
            )
        elif outcome == "updated":
            await self._labels.add_event(
                label_id=label_id,
                kind="updated",
                reason=f"claim refreshed: {claim.entity} ({claim.method})",
                actor=claim.source,
            )
            await self._resettle(label_id, claim)
        return outcome

    async def _resettle(self, label_id: int, claim: IntelClaim) -> None:
        """Re-derive an updated row's status from policy, both directions.

        - Untrusted method + currently active: whatever corroborated the OLD
          content did not corroborate the new — demote, clear the citation.
          If the new content genuinely still agrees with an active trusted
          source, the next reconcile re-promotes it with a fresh, truthful
          ``corroborated_by``; honest edits lose nothing but a cycle.
        - Trusted method + currently pending: the source now clears the bar
          on its own — activate, no corroborator needed or cited.
        """
        row = await self._labels.get_label(label_id)
        if row is None or row.status == "retired":
            return
        target = arrival_status(claim.method)
        if target == "pending" and row.status == "active":
            await self._labels.set_status(label_id, "pending", clear_corroboration=True)
            await self._labels.add_event(
                label_id=label_id,
                kind="demoted",
                reason="claim changed after activation — active status must be re-earned",
                actor=claim.source,
            )
        elif target == "active" and row.status == "pending":
            await self._labels.set_status(label_id, "active")
            await self._labels.add_event(
                label_id=label_id,
                kind="promoted",
                reason=f"method now {claim.method}: trusted on arrival",
                actor=claim.source,
            )

    async def reconcile(self) -> ReconcileResult:
        """The verification bot's cycle: demotions first, then promotions.

        Demote every untrusted-method active claim that no standing trusted
        source still corroborates — content drift and corroborator retirement
        both land here. Then promote every pending claim an independent
        trusted source now agrees with; because the check runs against
        CURRENT state, a report filed before its corroborating source was
        even harvested still promotes — arrival order must not decide
        verification. Demotions run first so a freshly orphaned claim cannot
        sit active through the promotion pass that follows.
        """
        result = ReconcileResult()
        for active in await self._labels.active_labels():
            if arrival_status(active.method) == "active":
                continue  # trusted claims stand on their method, not on peers
            if await self._corroborator_for(active) is not None:
                continue
            await self._labels.set_status(active.id, "pending", clear_corroboration=True)
            await self._labels.add_event(
                label_id=active.id,
                kind="demoted",
                reason=(
                    "corroboration no longer holds"
                    + (
                        f" (was corroborated by {active.corroborated_by})"
                        if active.corroborated_by
                        else ""
                    )
                ),
                actor=CORROBORATOR,
            )
            result.demoted.append(active.id)
            logger.info(
                "demoted label %d (%s %s): corroboration no longer holds",
                active.id,
                active.chain,
                active.address,
            )

        for pending in await self._labels.pending_labels():
            candidate = await self._corroborator_for(pending)
            if candidate is None:
                continue
            await self._labels.set_status(pending.id, "active", corroborated_by=candidate.source)
            await self._labels.add_event(
                label_id=pending.id,
                kind="promoted",
                reason=(
                    f"corroborated by {candidate.source}: "
                    f"'{candidate.entity}' agrees with '{pending.entity}'"
                ),
                actor=CORROBORATOR,
            )
            result.promoted.append(pending.id)
            logger.info(
                "promoted label %d (%s %s): corroborated by %s",
                pending.id,
                pending.chain,
                pending.address,
                candidate.source,
            )
        return result

    async def _corroborator_for(self, claimant: StoredLabel) -> StoredLabel | None:
        for candidate in await self._labels.claims_for(claimant.chain, claimant.address):
            if corroborates(claimant, candidate):
                return candidate
        return None

    async def retire(self, label: StoredLabel, *, reason: str, actor: str) -> None:
        """Retirement is always explicit and always evented — there is no
        implicit path out of ``active`` except demotion, and none at all out
        of ``retired``. Claims this label was corroborating are not touched
        here: the next reconcile demotes them, so the cascade is one cycle
        behind and fully on the record.
        """
        await self._labels.set_status(label.id, "retired")
        await self._labels.add_event(label_id=label.id, kind="retired", reason=reason, actor=actor)
