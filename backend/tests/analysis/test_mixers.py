"""Mixer exit heuristics — the direction, the ladder, and the mandatory caveat.

Three regressions dominate this file.

**Direction.** A backward crossing that enumerates the wrong end of the pool
produces output identical in shape to a working one — same rung, same
confidence, same weakness — and entirely wrong addresses. Every test in
``TestDirectionIsLoadBearing`` is written so that swapping the two directions,
or flipping the sign of a single time comparison, turns it red. Each rung is
then exercised in both directions for the same reason.

**Refusal.** Rung 4 declines when two parties share a fingerprint, and rung 2
declines when the only "link" runs through the mixer contract itself. Those
are the tests that stop this module from promoting an anonymity set into a
named link.

**The caveat.** A candidate with no weakness is indistinguishable in a report
from a signature-verified hop. It must be unconstructable, not merely absent.
"""

from __future__ import annotations

from dataclasses import replace
from datetime import UTC, datetime, timedelta

import pytest

from cipherchain.analysis.mixers import (
    ADDRESS_MATCH_HEURISTIC,
    ANONYMITY_SET_HEURISTIC,
    ANONYMITY_SET_MAX_CONFIDENCE,
    GAS_PRICE_HEURISTIC,
    LINKED_ADDRESS_HEURISTIC,
    MAX_FOLLOW,
    MAX_MIXER_CONFIDENCE,
    MIXER_EXIT_LADDER,
    MIXER_WINDOW,
    MULTI_DENOMINATION_HEURISTIC,
    DirectInteraction,
    MixerActivity,
    MixerCandidate,
    MixerDeposit,
    MixerExitResult,
    MixerWithdrawal,
    trace_back_from_withdrawal,
    trace_forward_from_deposit,
)
from cipherchain.core.models import Direction

T0 = datetime(2026, 3, 1, 12, 0, tzinfo=UTC)
POOL = "tornado-eth-100"
OTHER_POOL = "tornado-eth-1"
SUBJECT = 99  # the address being traced; never a candidate's address


def at(hours: float = 0.0, *, days: float = 0.0) -> datetime:
    return T0 + timedelta(hours=hours, days=days)


def dep(
    tx_hash: str,
    address_id: int,
    when: datetime,
    *,
    pool: str = POOL,
    denomination: int = 100,
    gas_price: int | None = None,
) -> MixerDeposit:
    return MixerDeposit(
        tx_hash=tx_hash,
        address_id=address_id,
        pool=pool,
        denomination=denomination,
        timestamp=when,
        gas_price=gas_price,
    )


def wd(
    tx_hash: str,
    address_id: int,
    when: datetime,
    *,
    pool: str = POOL,
    denomination: int = 100,
    gas_price: int | None = None,
) -> MixerWithdrawal:
    return MixerWithdrawal(
        tx_hash=tx_hash,
        address_id=address_id,
        pool=pool,
        denomination=denomination,
        timestamp=when,
        gas_price=gas_price,
    )


def hashes(result: MixerExitResult) -> list[str]:
    return [candidate.tx_hash for candidate in result.candidates]


# ── direction ─────────────────────────────────────────────────────────────


class TestDirectionIsLoadBearing:
    """Swap the two directions and every test here goes red.

    Each fixture deliberately contains all four combinations — a deposit and a
    withdrawal on either side of the anchor in time — so a result that picked
    the wrong end of the pool, or the wrong sign of the comparison, names a
    transaction that is present in the input and obviously wrong in the
    assertion.
    """

    ACTIVITY = MixerActivity(
        deposits=(dep("0xdep-before", 1, at(-5)), dep("0xdep-after", 2, at(5))),
        withdrawals=(
            wd("0xwd-before", 3, at(-4)),
            wd("0xanchor", SUBJECT, T0),
            wd("0xwd-after", 4, at(4)),
        ),
    )
    ANCHOR_WITHDRAWAL = wd("0xanchor", SUBJECT, T0)
    ANCHOR_DEPOSIT = dep("0xanchor-dep", SUBJECT, T0)

    def test_backward_from_a_withdrawal_offers_only_deposits_that_came_first(self) -> None:
        """Money arriving at T can only have been funded before T, and only
        by a deposit. A withdrawal is never a funding source."""
        result = trace_back_from_withdrawal(self.ANCHOR_WITHDRAWAL, self.ACTIVITY)
        assert hashes(result) == ["0xdep-before"]
        assert result.direction is Direction.BACKWARD
        assert all(c.direction is Direction.BACKWARD for c in result.candidates)

    def test_forward_from_a_deposit_offers_only_withdrawals_that_came_after(self) -> None:
        """Money deposited at T can only leave after T, and only as a
        withdrawal. A deposit is never a cash-out."""
        activity = replace(
            self.ACTIVITY,
            deposits=(*self.ACTIVITY.deposits, self.ANCHOR_DEPOSIT),
        )
        result = trace_forward_from_deposit(self.ANCHOR_DEPOSIT, activity)
        assert hashes(result) == ["0xwd-after"]
        assert result.direction is Direction.FORWARD
        assert all(c.direction is Direction.FORWARD for c in result.candidates)

    def test_the_two_directions_over_one_pool_never_name_the_same_transaction(self) -> None:
        """Backward reads deposits, forward reads withdrawals. An overlap
        would mean one of the two is reading the wrong end of the pool."""
        activity = replace(
            self.ACTIVITY,
            deposits=(*self.ACTIVITY.deposits, self.ANCHOR_DEPOSIT),
        )
        backward = trace_back_from_withdrawal(self.ANCHOR_WITHDRAWAL, activity)
        forward = trace_forward_from_deposit(self.ANCHOR_DEPOSIT, activity)
        assert set(hashes(backward)).isdisjoint(hashes(forward))

    def test_an_event_at_the_anchors_own_timestamp_belongs_to_neither_direction(self) -> None:
        """Strict on both sides. A same-instant event cannot have funded the
        withdrawal, and admitting it would let one transaction be a candidate
        for the same anchor whichever way the trace runs."""
        simultaneous = MixerActivity(
            deposits=(dep("0xsame-instant", 1, T0),),
            withdrawals=(wd("0xsame-instant-wd", 2, T0), self.ANCHOR_WITHDRAWAL),
        )
        backward = trace_back_from_withdrawal(self.ANCHOR_WITHDRAWAL, simultaneous)
        forward = trace_forward_from_deposit(self.ANCHOR_DEPOSIT, simultaneous)
        assert backward.candidates == () and backward.rung is None
        assert forward.candidates == () and forward.rung is None

    def test_the_backward_observation_says_before_and_the_forward_one_says_after(self) -> None:
        """The honest-nothing sentence is the only thing a reader sees when a
        crossing refuses; it must not describe the opposite direction."""
        empty = MixerActivity()
        assert "before" in trace_back_from_withdrawal(self.ANCHOR_WITHDRAWAL, empty).observation
        assert "after" in trace_forward_from_deposit(self.ANCHOR_DEPOSIT, empty).observation


# ── rung 1: address match ─────────────────────────────────────────────────


class TestAddressMatch:
    def test_one_address_on_both_sides_of_the_pool_resolves_backward(self) -> None:
        activity = MixerActivity(
            deposits=(dep("0xmine", SUBJECT, at(-30)), dep("0xstranger", 7, at(-20))),
            withdrawals=(wd("0xanchor", SUBJECT, T0),),
        )
        result = trace_back_from_withdrawal(wd("0xanchor", SUBJECT, T0), activity)
        assert result.rung == ADDRESS_MATCH_HEURISTIC
        assert hashes(result) == ["0xmine"]
        assert result.candidates[0].address_id == SUBJECT

    def test_one_address_on_both_sides_of_the_pool_resolves_forward(self) -> None:
        activity = MixerActivity(
            deposits=(dep("0xanchor", SUBJECT, T0),),
            withdrawals=(wd("0xmine", SUBJECT, at(30)), wd("0xstranger", 7, at(20))),
        )
        result = trace_forward_from_deposit(dep("0xanchor", SUBJECT, T0), activity)
        assert result.rung == ADDRESS_MATCH_HEURISTIC
        assert hashes(result) == ["0xmine"]

    def test_the_matching_address_on_the_wrong_side_of_time_does_not_fire(self) -> None:
        """The subject depositing AFTER the withdrawal it received cannot have
        funded it. Direction outranks identity."""
        anchor = wd("0xanchor", SUBJECT, T0)
        activity = MixerActivity(
            deposits=(dep("0xlater", SUBJECT, at(30)), dep("0xstranger", 7, at(-20))),
            withdrawals=(anchor,),
        )
        result = trace_back_from_withdrawal(anchor, activity)
        assert result.rung == ANONYMITY_SET_HEURISTIC
        assert hashes(result) == ["0xstranger"]

    def test_the_matching_address_in_another_pool_does_not_fire(self) -> None:
        """A 100 ETH withdrawal cannot come from a 1 ETH deposit, whoever
        made it — the denominations do not add up."""
        anchor = wd("0xanchor", SUBJECT, T0)
        activity = MixerActivity(
            deposits=(dep("0xwrong-pool", SUBJECT, at(-30), pool=OTHER_POOL),),
            withdrawals=(anchor,),
        )
        result = trace_back_from_withdrawal(anchor, activity)
        assert result.candidates == ()
        assert result.rung is None

    def test_an_address_match_beats_a_large_crowd(self) -> None:
        """Resolving by identity must not also spend budget enumerating the
        anonymity set — the crowd is still reported, not followed."""
        anchor = wd("0xanchor", SUBJECT, T0)
        crowd = tuple(dep(f"0xnoise{i}", 500 + i, at(-i - 1)) for i in range(40))
        activity = MixerActivity(
            deposits=(*crowd, dep("0xmine", SUBJECT, at(-30))),
            withdrawals=(anchor,),
        )
        result = trace_back_from_withdrawal(anchor, activity)
        assert result.rung == ADDRESS_MATCH_HEURISTIC
        assert hashes(result) == ["0xmine"]
        assert result.anonymity_set == 41  # reported honestly, just not followed
        assert result.capped is False

    def test_the_strongest_rung_is_still_capped_below_certainty(self) -> None:
        """An address match proves what the ADDRESS did. If that address is a
        custodial deposit address, it names the service, not the subject."""
        anchor = wd("0xanchor", SUBJECT, T0)
        activity = MixerActivity(
            deposits=(dep("0xmine", SUBJECT, at(-30)),), withdrawals=(anchor,)
        )
        result = trace_back_from_withdrawal(anchor, activity)
        assert result.candidates[0].confidence == MAX_MIXER_CONFIDENCE
        assert result.candidates[0].confidence < 1.0

    def test_several_deposits_by_the_matching_address_still_name_one_address(self) -> None:
        """Three deposits by one address is three transactions and ONE story,
        so the confidence is not split — there is nothing to split it over."""
        anchor = wd("0xanchor", SUBJECT, T0)
        activity = MixerActivity(
            deposits=(
                dep("0xmine-a", SUBJECT, at(-30)),
                dep("0xmine-b", SUBJECT, at(-20)),
                dep("0xmine-c", SUBJECT, at(-10)),
            ),
            withdrawals=(anchor,),
        )
        result = trace_back_from_withdrawal(anchor, activity)
        assert sorted(hashes(result)) == ["0xmine-a", "0xmine-b", "0xmine-c"]
        assert {c.confidence for c in result.candidates} == {MAX_MIXER_CONFIDENCE}
        assert all("different addresses" not in c.weakness for c in result.candidates)


# ── rung 2: linked address ────────────────────────────────────────────────


class TestLinkedAddress:
    ANCHOR = wd("0xanchor", SUBJECT, T0)

    def test_a_transfer_outside_the_mixer_links_a_depositor_to_the_withdrawer(self) -> None:
        activity = MixerActivity(
            deposits=(dep("0xfriend", 5, at(-30)), dep("0xstranger", 7, at(-20))),
            withdrawals=(self.ANCHOR,),
            interactions=(DirectInteraction("0xpayment", 5, SUBJECT),),
        )
        result = trace_back_from_withdrawal(self.ANCHOR, activity)
        assert result.rung == LINKED_ADDRESS_HEURISTIC
        assert hashes(result) == ["0xfriend"]
        assert "0xpayment" in result.candidates[0].refs

    def test_the_link_counts_in_either_orientation(self) -> None:
        """Who paid whom is irrelevant: that the two addresses transacted at
        all is the entire signal."""
        activity = MixerActivity(
            deposits=(dep("0xfriend", 5, at(-30)),),
            withdrawals=(self.ANCHOR,),
            interactions=(DirectInteraction("0xpayment", SUBJECT, 5),),
        )
        result = trace_back_from_withdrawal(self.ANCHOR, activity)
        assert result.rung == LINKED_ADDRESS_HEURISTIC
        assert hashes(result) == ["0xfriend"]

    def test_the_mixer_contract_never_wins_this_rung(self) -> None:
        """The guard. A router appears on both sides of its own pool and
        transacts directly with every user it serves, so left in the candidate
        set it beats every real depositor and the crossing 'resolves' by
        naming the mixer as the source of the money — for everybody."""
        router = 4242
        activity = MixerActivity(
            deposits=(dep("0xrouter", router, at(-30)), dep("0xstranger", 5, at(-20))),
            withdrawals=(self.ANCHOR,),
            interactions=(DirectInteraction("0xwithdraw-call", router, SUBJECT),),
            mixer_address_ids=frozenset({router}),
        )
        result = trace_back_from_withdrawal(self.ANCHOR, activity)
        assert result.rung == ANONYMITY_SET_HEURISTIC
        assert hashes(result) == ["0xstranger"]

    def test_the_mixers_own_transactions_are_not_part_of_the_crowd_either(self) -> None:
        """Counting the pool contract's own movements inflates the anonymity
        set and offers a follow-up that leads straight back into the pool."""
        router = 4242
        activity = MixerActivity(
            deposits=(
                dep("0xrouter", router, at(-30)),
                dep("0xa", 5, at(-20)),
                dep("0xb", 6, at(-10)),
            ),
            withdrawals=(self.ANCHOR,),
            mixer_address_ids=frozenset({router}),
        )
        result = trace_back_from_withdrawal(self.ANCHOR, activity)
        assert result.anonymity_set == 2
        assert "0xrouter" not in hashes(result)

    def test_a_shared_intermediary_is_not_a_link(self) -> None:
        """Links do not chain. 'Both parties paid the same exchange' connects
        most of a pool to most of the rest of it."""
        activity = MixerActivity(
            deposits=(dep("0xstranger", 5, at(-30)),),
            withdrawals=(self.ANCHOR,),
            interactions=(
                DirectInteraction("0xpay-in", 5, 777),
                DirectInteraction("0xpay-out", 777, SUBJECT),
            ),
        )
        result = trace_back_from_withdrawal(self.ANCHOR, activity)
        assert result.rung == ANONYMITY_SET_HEURISTIC

    def test_an_interaction_with_an_uninvolved_third_party_links_nobody(self) -> None:
        activity = MixerActivity(
            deposits=(dep("0xstranger", 5, at(-30)),),
            withdrawals=(self.ANCHOR,),
            interactions=(DirectInteraction("0xpayment", 11, 12),),
        )
        result = trace_back_from_withdrawal(self.ANCHOR, activity)
        assert result.rung == ANONYMITY_SET_HEURISTIC

    def test_two_linked_candidates_split_the_confidence_and_say_so(self) -> None:
        """Two links is two mutually exclusive stories. At most one is true,
        and the weakness has to say that out loud."""
        activity = MixerActivity(
            deposits=(dep("0xfriend-a", 5, at(-30)), dep("0xfriend-b", 6, at(-20))),
            withdrawals=(self.ANCHOR,),
            interactions=(
                DirectInteraction("0xpay-a", 5, SUBJECT),
                DirectInteraction("0xpay-b", SUBJECT, 6),
            ),
        )
        result = trace_back_from_withdrawal(self.ANCHOR, activity)
        assert result.rung == LINKED_ADDRESS_HEURISTIC
        assert sorted(hashes(result)) == ["0xfriend-a", "0xfriend-b"]
        assert {c.confidence for c in result.candidates} == {0.35}
        assert all("2 different addresses" in c.weakness for c in result.candidates)

    def test_linked_address_fires_forward_too(self) -> None:
        anchor = dep("0xanchor", SUBJECT, T0)
        activity = MixerActivity(
            deposits=(anchor,),
            withdrawals=(wd("0xfriend", 5, at(30)), wd("0xstranger", 7, at(20))),
            interactions=(DirectInteraction("0xpayment", 5, SUBJECT),),
        )
        result = trace_forward_from_deposit(anchor, activity)
        assert result.rung == LINKED_ADDRESS_HEURISTIC
        assert hashes(result) == ["0xfriend"]

    def test_a_link_to_a_withdrawal_before_the_deposit_does_not_fire_forward(self) -> None:
        """A linked address that withdrew before the subject deposited cannot
        have withdrawn the subject's money."""
        anchor = dep("0xanchor", SUBJECT, T0)
        activity = MixerActivity(
            deposits=(anchor,),
            withdrawals=(wd("0xfriend", 5, at(-30)), wd("0xstranger", 7, at(20))),
            interactions=(DirectInteraction("0xpayment", 5, SUBJECT),),
        )
        result = trace_forward_from_deposit(anchor, activity)
        assert result.rung == ANONYMITY_SET_HEURISTIC
        assert hashes(result) == ["0xstranger"]


# ── rung 3: unique gas price ──────────────────────────────────────────────


class TestUniqueGasPrice:
    ANCHOR = wd("0xanchor", SUBJECT, T0, gas_price=31_337_000_000)

    def test_a_price_appearing_once_on_each_side_names_a_deposit(self) -> None:
        activity = MixerActivity(
            deposits=(
                dep("0xhand-set", 5, at(-30), gas_price=31_337_000_000),
                dep("0xdefault", 7, at(-20), gas_price=20_000_000_000),
            ),
            withdrawals=(self.ANCHOR,),
        )
        result = trace_back_from_withdrawal(self.ANCHOR, activity)
        assert result.rung == GAS_PRICE_HEURISTIC
        assert hashes(result) == ["0xhand-set"]
        assert "31337000000" in result.candidates[0].weakness

    def test_a_price_shared_by_two_deposits_names_nobody(self) -> None:
        """Two candidates for one price is not a fingerprint; it identifies
        neither of them, so the rung declines rather than picking."""
        activity = MixerActivity(
            deposits=(
                dep("0xa", 5, at(-30), gas_price=31_337_000_000),
                dep("0xb", 7, at(-20), gas_price=31_337_000_000),
            ),
            withdrawals=(self.ANCHOR,),
        )
        result = trace_back_from_withdrawal(self.ANCHOR, activity)
        assert result.rung == ANONYMITY_SET_HEURISTIC

    def test_a_price_shared_by_two_withdrawals_is_a_client_default(self) -> None:
        """If several withdrawals carry the same price it was not typed by
        hand — it is whatever some wallet fills in, and matches nobody."""
        activity = MixerActivity(
            deposits=(dep("0xa", 5, at(-30), gas_price=31_337_000_000),),
            withdrawals=(
                self.ANCHOR,
                wd("0xother", 8, at(-2), gas_price=31_337_000_000),
            ),
        )
        result = trace_back_from_withdrawal(self.ANCHOR, activity)
        assert result.rung == ANONYMITY_SET_HEURISTIC

    def test_uniqueness_is_measured_across_the_whole_mixer_not_one_pool(self) -> None:
        """A price used on a deposit into a different pool is still evidence
        that the price is not distinctive. Scoping the check to one pool would
        let a wallet default pass as a hand-set number."""
        activity = MixerActivity(
            deposits=(
                dep("0xa", 5, at(-30), gas_price=31_337_000_000),
                dep("0xelsewhere", 7, at(-20), pool=OTHER_POOL, gas_price=31_337_000_000),
            ),
            withdrawals=(self.ANCHOR,),
        )
        result = trace_back_from_withdrawal(self.ANCHOR, activity)
        assert result.rung == ANONYMITY_SET_HEURISTIC

    def test_a_unique_price_on_a_deposit_after_the_withdrawal_does_not_fire(self) -> None:
        activity = MixerActivity(
            deposits=(
                dep("0xlater", 5, at(30), gas_price=31_337_000_000),
                dep("0xstranger", 7, at(-20)),
            ),
            withdrawals=(self.ANCHOR,),
        )
        result = trace_back_from_withdrawal(self.ANCHOR, activity)
        assert result.rung == ANONYMITY_SET_HEURISTIC
        assert hashes(result) == ["0xstranger"]

    def test_a_missing_gas_price_never_matches_another_missing_one(self) -> None:
        """The None/None trap: absent data must not read as a match, or this
        rung fires on every chain that does not report gas."""
        anchor = wd("0xanchor", SUBJECT, T0)
        activity = MixerActivity(
            deposits=(dep("0xa", 5, at(-30)),), withdrawals=(anchor,)
        )
        result = trace_back_from_withdrawal(anchor, activity)
        assert result.rung == ANONYMITY_SET_HEURISTIC

    def test_a_zero_gas_price_carries_no_signal(self) -> None:
        anchor = wd("0xanchor", SUBJECT, T0, gas_price=0)
        activity = MixerActivity(
            deposits=(dep("0xa", 5, at(-30), gas_price=0),), withdrawals=(anchor,)
        )
        result = trace_back_from_withdrawal(anchor, activity)
        assert result.rung == ANONYMITY_SET_HEURISTIC

    def test_unique_gas_price_fires_forward_too(self) -> None:
        anchor = dep("0xanchor", SUBJECT, T0, gas_price=31_337_000_000)
        activity = MixerActivity(
            deposits=(anchor,),
            withdrawals=(
                wd("0xhand-set", 5, at(30), gas_price=31_337_000_000),
                wd("0xdefault", 7, at(20), gas_price=20_000_000_000),
            ),
        )
        result = trace_forward_from_deposit(anchor, activity)
        assert result.rung == GAS_PRICE_HEURISTIC
        assert hashes(result) == ["0xhand-set"]


# ── rung 4: multi-denomination fingerprint ────────────────────────────────


class TestMultiDenomination:
    ANCHOR = wd("0xanchor", SUBJECT, T0)
    OWN_SECOND_LEG = wd("0xanchor-2", SUBJECT, at(1), pool=OTHER_POOL, denomination=1)

    def test_a_two_pool_fingerprint_matched_by_exactly_one_depositor_fires(self) -> None:
        activity = MixerActivity(
            deposits=(
                dep("0xmatch-100", 5, at(-30)),
                dep("0xmatch-1", 5, at(-29), pool=OTHER_POOL, denomination=1),
                dep("0xstranger", 7, at(-20)),
            ),
            withdrawals=(self.ANCHOR, self.OWN_SECOND_LEG),
        )
        result = trace_back_from_withdrawal(self.ANCHOR, activity)
        assert result.rung == MULTI_DENOMINATION_HEURISTIC
        # The candidate is the leg in the ANCHOR's pool — the only one whose
        # denomination can account for the withdrawal being traced.
        assert hashes(result) == ["0xmatch-100"]
        assert result.candidates[0].address_id == 5
        assert result.candidates[0].pool == POOL

    def test_the_returned_leg_is_the_one_in_the_anchors_pool_not_the_largest(self) -> None:
        """The fingerprint spans pools; the candidate must not. Handing back
        the matched party's *biggest* leg would follow a 100 ETH deposit as
        the source of a 1 ETH withdrawal — a denomination that cannot account
        for the money being traced, and the ranking would hide it because
        ranking puts value first."""
        anchor = wd("0xanchor", SUBJECT, T0, pool=OTHER_POOL, denomination=1)
        activity = MixerActivity(
            deposits=(
                dep("0xf-1", 5, at(-30), pool=OTHER_POOL, denomination=1),
                dep("0xf-100", 5, at(-29), pool=POOL, denomination=100),
            ),
            withdrawals=(anchor, wd("0xanchor-2", SUBJECT, at(1), pool=POOL, denomination=100)),
        )
        result = trace_back_from_withdrawal(anchor, activity)
        assert result.rung == MULTI_DENOMINATION_HEURISTIC
        assert hashes(result) == ["0xf-1"]
        assert result.candidates[0].pool == OTHER_POOL
        assert result.candidates[0].value == 1

    def test_a_fingerprint_shared_by_two_depositors_is_refused_not_broken(self) -> None:
        """A shape two people made is not a fingerprint. Choosing between them
        would publish a name derived from a signal just shown to be
        non-distinctive, so the rung declines and the crowd answers instead."""
        activity = MixerActivity(
            deposits=(
                dep("0xa-100", 5, at(-30)),
                dep("0xa-1", 5, at(-29), pool=OTHER_POOL, denomination=1),
                dep("0xb-100", 6, at(-25)),
                dep("0xb-1", 6, at(-24), pool=OTHER_POOL, denomination=1),
            ),
            withdrawals=(self.ANCHOR, self.OWN_SECOND_LEG),
        )
        result = trace_back_from_withdrawal(self.ANCHOR, activity)
        assert result.rung == ANONYMITY_SET_HEURISTIC
        assert sorted(hashes(result)) == ["0xa-100", "0xb-100"]

    def test_a_single_pool_fingerprint_is_not_distinctive_enough_to_match(self) -> None:
        """'Withdrew 100 ETH once' describes a crowd, not a person, even when
        exactly one depositor happens to share it."""
        activity = MixerActivity(
            deposits=(dep("0xonly", 5, at(-30)),),
            withdrawals=(self.ANCHOR,),
        )
        result = trace_back_from_withdrawal(self.ANCHOR, activity)
        assert result.rung == ANONYMITY_SET_HEURISTIC

    def test_the_fingerprint_must_match_exactly_not_merely_overlap(self) -> None:
        """A superset is a different shape. Matching on overlap would collapse
        the rung into 'used the same pool at some point'."""
        activity = MixerActivity(
            deposits=(
                dep("0xa-100", 5, at(-30)),
                dep("0xa-1", 5, at(-29), pool=OTHER_POOL, denomination=1),
                dep("0xa-extra", 5, at(-28), pool="tornado-eth-10", denomination=10),
            ),
            withdrawals=(self.ANCHOR, self.OWN_SECOND_LEG),
        )
        result = trace_back_from_withdrawal(self.ANCHOR, activity)
        assert result.rung == ANONYMITY_SET_HEURISTIC

    def test_multiplicity_is_part_of_the_fingerprint(self) -> None:
        """Three 10-ETH deposits and one 1-ETH is a different shape from one
        of each; a set-based comparison would wrongly call them equal."""
        anchor = wd("0xanchor", SUBJECT, T0, pool="tornado-eth-10", denomination=10)
        own = (
            anchor,
            wd("0xanchor-2", SUBJECT, at(1), pool="tornado-eth-10", denomination=10),
            wd("0xanchor-3", SUBJECT, at(2), pool=OTHER_POOL, denomination=1),
        )
        activity = MixerActivity(
            deposits=(
                dep("0xone-each", 5, at(-30), pool="tornado-eth-10", denomination=10),
                dep("0xone-each-b", 5, at(-29), pool=OTHER_POOL, denomination=1),
            ),
            withdrawals=own,
        )
        result = trace_back_from_withdrawal(anchor, activity)
        assert result.rung == ANONYMITY_SET_HEURISTIC

    def test_a_matching_fingerprint_on_the_wrong_side_of_time_does_not_fire(self) -> None:
        """Deposits made after the withdrawal cannot have funded it, however
        well the shape matches."""
        activity = MixerActivity(
            deposits=(
                dep("0xlate-100", 5, at(30)),
                dep("0xlate-1", 5, at(31), pool=OTHER_POOL, denomination=1),
                dep("0xstranger", 7, at(-20)),
            ),
            withdrawals=(self.ANCHOR, self.OWN_SECOND_LEG),
        )
        result = trace_back_from_withdrawal(self.ANCHOR, activity)
        assert result.rung == ANONYMITY_SET_HEURISTIC
        assert hashes(result) == ["0xstranger"]

    def test_multi_denomination_fires_forward_too(self) -> None:
        anchor = dep("0xanchor", SUBJECT, T0)
        activity = MixerActivity(
            deposits=(anchor, dep("0xanchor-2", SUBJECT, at(-1), pool=OTHER_POOL, denomination=1)),
            withdrawals=(
                wd("0xmatch-100", 5, at(30)),
                wd("0xmatch-1", 5, at(31), pool=OTHER_POOL, denomination=1),
                wd("0xstranger", 7, at(20)),
            ),
        )
        result = trace_forward_from_deposit(anchor, activity)
        assert result.rung == MULTI_DENOMINATION_HEURISTIC
        assert hashes(result) == ["0xmatch-100"]

    def test_a_party_that_never_touched_the_anchor_pool_cannot_match_the_shape(self) -> None:
        """The anchor is always part of its own fingerprint, so the anchor's
        pool is always in the shape being matched. A far party with no leg in
        that pool therefore has a different shape and cannot match — there is
        no path on which a fingerprint matches and then has nothing to hand
        back."""
        anchor = wd("0xanchor", SUBJECT, T0, pool="tornado-eth-10", denomination=10)
        activity = MixerActivity(
            deposits=(
                dep("0xa-1", 5, at(-30), pool=OTHER_POOL, denomination=1),
                dep("0xa-100", 5, at(-29), pool=POOL),
            ),
            withdrawals=(
                anchor,
                wd("0xanchor-2", SUBJECT, at(1), pool=OTHER_POOL, denomination=1),
                wd("0xanchor-3", SUBJECT, at(2), pool=POOL),
            ),
        )
        result = trace_back_from_withdrawal(anchor, activity)
        assert result.candidates == ()
        assert result.rung is None

    def test_strangers_on_the_anchors_own_side_are_not_part_of_its_shape(self) -> None:
        """A real pool's withdrawal list is mostly other people. The shape
        being matched is the *subject's*, so everyone else's legs on the same
        side have to be filtered out by address — otherwise the fingerprint
        grows with the size of the pool and the rung stops firing at all, or
        matches a shape nobody actually has.

        Every other fixture here puts only the subject on the own side, which
        is what lets that filter be deleted without a test noticing."""
        activity = MixerActivity(
            deposits=(
                dep("0xmatch-100", 5, at(-30)),
                dep("0xmatch-1", 5, at(-29), pool=OTHER_POOL, denomination=1),
                dep("0xstranger", 7, at(-20)),
            ),
            withdrawals=(
                self.ANCHOR,
                self.OWN_SECOND_LEG,
                # A different party, on the anchor's side of the pool.
                wd("0xnot-mine", 8, at(2), pool="tornado-eth-10", denomination=10),
            ),
        )
        result = trace_back_from_withdrawal(self.ANCHOR, activity)
        assert result.rung == MULTI_DENOMINATION_HEURISTIC
        assert hashes(result) == ["0xmatch-100"]
        assert "2 pools" in result.candidates[0].weakness

    def test_the_shape_is_built_only_from_events_on_the_right_side_of_time(self) -> None:
        """The quiet one. The far party's shape must be assembled from the
        events that could actually have funded the anchor, not from everything
        they ever did at this mixer.

        Here address 5 deposited into the anchor's pool before the withdrawal
        and into a second pool fifty hours *after* it. Counting that later
        deposit completes a two-pool shape matching the subject's, and the
        crossing stops being an anonymity set and becomes a named
        multi-denomination match — at a higher confidence, resting on a
        transaction that postdates the money it claims to explain.

        The wrong version of this returns a plausible answer rather than
        crashing, so only an assertion on the rung catches it."""
        anchor = wd("0xanchor", SUBJECT, T0)
        own_second_leg = wd("0xanchor-2", SUBJECT, at(1), pool=OTHER_POOL, denomination=1)
        activity = MixerActivity(
            deposits=(
                dep("0xeligible", 5, at(-10)),
                dep("0xafter-the-anchor", 5, at(50), pool=OTHER_POOL, denomination=1),
                dep("0xstranger", 7, at(-20)),
            ),
            withdrawals=(anchor, own_second_leg),
        )
        result = trace_back_from_withdrawal(anchor, activity)
        assert result.rung == ANONYMITY_SET_HEURISTIC
        assert sorted(hashes(result)) == ["0xeligible", "0xstranger"]
        assert all(c.confidence <= ANONYMITY_SET_MAX_CONFIDENCE for c in result.candidates)


# ── rung 5: the anonymity set ─────────────────────────────────────────────


class TestAnonymitySetFallback:
    ANCHOR = wd("0xanchor", SUBJECT, T0)

    def test_a_pool_with_no_identifying_signal_returns_the_whole_crowd(self) -> None:
        activity = MixerActivity(
            deposits=tuple(dep(f"0x{i}", 10 + i, at(-i - 1)) for i in range(4)),
            withdrawals=(self.ANCHOR,),
        )
        result = trace_back_from_withdrawal(self.ANCHOR, activity)
        assert result.rung == ANONYMITY_SET_HEURISTIC
        assert result.anonymity_set == 4
        assert len(result.candidates) == 4
        assert result.capped is False

    def test_confidence_is_one_over_the_size_of_the_set(self) -> None:
        activity = MixerActivity(
            deposits=tuple(dep(f"0x{i}", 10 + i, at(-i - 1)) for i in range(4)),
            withdrawals=(self.ANCHOR,),
        )
        result = trace_back_from_withdrawal(self.ANCHOR, activity)
        assert {c.confidence for c in result.candidates} == {0.25}

    def test_a_single_candidate_is_still_never_more_than_half(self) -> None:
        """A Tornado withdrawal is not paired to any deposit. One candidate in
        the window means 'one candidate', never 'certainty' — the deposit that
        funded it may sit months outside the window, or the subject may not
        have withdrawn at all."""
        activity = MixerActivity(
            deposits=(dep("0xonly", 5, at(-30)),), withdrawals=(self.ANCHOR,)
        )
        result = trace_back_from_withdrawal(self.ANCHOR, activity)
        assert result.candidates[0].confidence == ANONYMITY_SET_MAX_CONFIDENCE

    def test_deposits_outside_the_window_are_not_in_the_set(self) -> None:
        activity = MixerActivity(
            deposits=(dep("0xinside", 5, at(days=-6)), dep("0xoutside", 6, at(days=-8))),
            withdrawals=(self.ANCHOR,),
        )
        result = trace_back_from_withdrawal(self.ANCHOR, activity)
        assert hashes(result) == ["0xinside"]
        assert result.anonymity_set == 1

    def test_withdrawals_outside_the_forward_window_are_not_in_the_set(self) -> None:
        anchor = dep("0xanchor", SUBJECT, T0)
        activity = MixerActivity(
            deposits=(anchor,),
            withdrawals=(wd("0xinside", 5, at(days=6)), wd("0xoutside", 6, at(days=8))),
        )
        result = trace_forward_from_deposit(anchor, activity)
        assert hashes(result) == ["0xinside"]

    def test_the_window_edge_itself_is_inside_the_set(self) -> None:
        """Δ is inclusive at exactly the boundary, in both directions. Left
        unpinned, a refactor can flip the comparison and silently shrink every
        anonymity set in the case by one edge event."""
        anchor = wd("0xanchor", SUBJECT, T0)
        backward = MixerActivity(
            deposits=(
                dep("0xon-the-edge", 5, T0 - MIXER_WINDOW),
                dep("0xjust-past", 6, T0 - MIXER_WINDOW - timedelta(microseconds=1)),
            ),
            withdrawals=(anchor,),
        )
        result = trace_back_from_withdrawal(anchor, backward)
        assert hashes(result) == ["0xon-the-edge"]

        anchor_deposit = dep("0xanchor-dep", SUBJECT, T0)
        forward = MixerActivity(
            deposits=(anchor_deposit,),
            withdrawals=(
                wd("0xon-the-edge", 5, T0 + MIXER_WINDOW),
                wd("0xjust-past", 6, T0 + MIXER_WINDOW + timedelta(microseconds=1)),
            ),
        )
        assert hashes(trace_forward_from_deposit(anchor_deposit, forward)) == ["0xon-the-edge"]

    def test_a_very_wide_match_floors_above_zero_instead_of_raising(self) -> None:
        """Damping divides a rung's confidence by the number of addresses it
        named. Past about 1400 the quotient rounds to zero, and the candidate
        constructor rejects a zero confidence — so without the floor a pool
        this crowded would raise instead of returning very weak leads."""
        wide = 1401
        activity = MixerActivity(
            deposits=tuple(dep(f"0x{i:05d}", 1000 + i, at(-1)) for i in range(wide)),
            withdrawals=(self.ANCHOR,),
            interactions=tuple(
                DirectInteraction(f"0xpay{i:05d}", 1000 + i, SUBJECT) for i in range(wide)
            ),
        )
        result = trace_back_from_withdrawal(self.ANCHOR, activity, max_follow=wide)
        assert result.rung == LINKED_ADDRESS_HEURISTIC
        assert len(result.candidates) == wide
        assert {c.confidence for c in result.candidates} == {0.001}

    def test_the_set_is_ranked_by_value(self) -> None:
        activity = MixerActivity(
            deposits=(
                dep("0xsmall", 5, at(-1), denomination=1),
                dep("0xlarge", 6, at(-2), denomination=100),
                dep("0xmedium", 7, at(-3), denomination=10),
            ),
            withdrawals=(self.ANCHOR,),
        )
        result = trace_back_from_withdrawal(self.ANCHOR, activity)
        assert hashes(result) == ["0xlarge", "0xmedium", "0xsmall"]

    def test_equal_value_candidates_are_ordered_by_closeness_to_the_anchor(self) -> None:
        """Inside a real Tornado pool every candidate carries the same
        denomination, so the tie-break is what actually orders the follow-ups."""
        activity = MixerActivity(
            deposits=(
                dep("0xfar", 5, at(-40)),
                dep("0xnear", 6, at(-2)),
                dep("0xmid", 7, at(-20)),
            ),
            withdrawals=(self.ANCHOR,),
        )
        result = trace_back_from_withdrawal(self.ANCHOR, activity)
        assert hashes(result) == ["0xnear", "0xmid", "0xfar"]

    def test_the_follow_cap_truncates_and_the_weakness_admits_it(self) -> None:
        activity = MixerActivity(
            deposits=tuple(dep(f"0x{i:03d}", 100 + i, at(-i - 1), denomination=i + 1)
                           for i in range(5)),
            withdrawals=(self.ANCHOR,),
        )
        result = trace_back_from_withdrawal(self.ANCHOR, activity, max_follow=3)
        assert result.anonymity_set == 5
        assert len(result.candidates) == 3
        assert result.capped is True
        assert hashes(result) == ["0x004", "0x003", "0x002"]  # the three largest
        assert all("may not be among them" in c.weakness for c in result.candidates)
        assert all("one of 5" in c.weakness for c in result.candidates)

    def test_the_default_cap_is_max_follow(self) -> None:
        activity = MixerActivity(
            deposits=tuple(dep(f"0x{i:03d}", 100 + i, at(-i - 1)) for i in range(MAX_FOLLOW + 5)),
            withdrawals=(self.ANCHOR,),
        )
        result = trace_back_from_withdrawal(self.ANCHOR, activity)
        assert result.anonymity_set == MAX_FOLLOW + 5
        assert len(result.candidates) == MAX_FOLLOW
        assert result.capped is True

    def test_an_uncapped_set_does_not_claim_to_be_capped(self) -> None:
        activity = MixerActivity(
            deposits=(dep("0xa", 5, at(-1)), dep("0xb", 6, at(-2))),
            withdrawals=(self.ANCHOR,),
        )
        result = trace_back_from_withdrawal(self.ANCHOR, activity)
        assert result.capped is False
        assert all("may not be among them" not in c.weakness for c in result.candidates)


# ── refusals ──────────────────────────────────────────────────────────────


class TestTheHonestNothing:
    ANCHOR = wd("0xanchor", SUBJECT, T0)

    def test_an_empty_pool_returns_a_refusal_with_a_sentence(self) -> None:
        """A refusal is a real result and needs printable text; a blank here
        renders as a hole where a reader expects a finding."""
        result = trace_back_from_withdrawal(self.ANCHOR, MixerActivity())
        assert result.candidates == ()
        assert result.rung is None
        assert result.anonymity_set == 0
        assert result.resolved is False
        assert "nothing to follow" in result.observation

    def test_a_pool_holding_only_wrong_direction_events_returns_nothing(self) -> None:
        activity = MixerActivity(
            deposits=(dep("0xlater", 5, at(30)),), withdrawals=(self.ANCHOR,)
        )
        result = trace_back_from_withdrawal(self.ANCHOR, activity)
        assert result.candidates == ()
        assert result.rung is None

    def test_a_pool_holding_only_other_pools_events_returns_nothing(self) -> None:
        activity = MixerActivity(
            deposits=(dep("0xelsewhere", 5, at(-30), pool=OTHER_POOL, denomination=1),),
            withdrawals=(self.ANCHOR,),
        )
        result = trace_back_from_withdrawal(self.ANCHOR, activity)
        assert result.candidates == ()

    def test_every_result_carries_an_observation_including_refusals(self) -> None:
        activities = (
            MixerActivity(),
            MixerActivity(deposits=(dep("0xa", 5, at(-1)),)),
            MixerActivity(deposits=(dep("0xa", SUBJECT, at(-1)),)),
        )
        for activity in activities:
            result = trace_back_from_withdrawal(self.ANCHOR, activity)
            assert result.observation.strip()


# ── ladder order ──────────────────────────────────────────────────────────


class TestLadderOrder:
    """Strongest first, first fire wins, one rung per result.

    A run that resolves a pool by address match must never also spend budget
    enumerating a crowd, and a result carrying two rungs would leave a reader
    with no single confidence to act on.
    """

    ANCHOR = wd("0xanchor", SUBJECT, T0, gas_price=31_337_000_000)
    OWN_SECOND_LEG = wd("0xanchor-2", SUBJECT, at(1), pool=OTHER_POOL, denomination=1)

    def _everything_fires(self) -> MixerActivity:
        return MixerActivity(
            deposits=(
                dep("0xaddress-match", SUBJECT, at(-40)),
                dep("0xlinked", 5, at(-35)),
                dep("0xgas", 6, at(-30), gas_price=31_337_000_000),
                dep("0xfinger-100", 7, at(-25)),
                dep("0xfinger-1", 7, at(-24), pool=OTHER_POOL, denomination=1),
            ),
            withdrawals=(self.ANCHOR, self.OWN_SECOND_LEG),
            interactions=(DirectInteraction("0xpayment", 5, SUBJECT),),
        )

    def test_address_match_pre_empts_every_weaker_rung(self) -> None:
        result = trace_back_from_withdrawal(self.ANCHOR, self._everything_fires())
        assert result.rung == ADDRESS_MATCH_HEURISTIC
        assert hashes(result) == ["0xaddress-match"]

    def test_linked_address_pre_empts_gas_price_and_below(self) -> None:
        activity = self._everything_fires()
        activity = replace(
            activity, deposits=tuple(d for d in activity.deposits if d.address_id != SUBJECT)
        )
        result = trace_back_from_withdrawal(self.ANCHOR, activity)
        assert result.rung == LINKED_ADDRESS_HEURISTIC
        assert hashes(result) == ["0xlinked"]

    def test_gas_price_pre_empts_the_fingerprint_and_the_crowd(self) -> None:
        activity = self._everything_fires()
        activity = replace(
            activity,
            deposits=tuple(d for d in activity.deposits if d.address_id != SUBJECT),
            interactions=(),
        )
        result = trace_back_from_withdrawal(self.ANCHOR, activity)
        assert result.rung == GAS_PRICE_HEURISTIC
        assert hashes(result) == ["0xgas"]

    def test_the_fingerprint_pre_empts_the_crowd(self) -> None:
        activity = self._everything_fires()
        activity = replace(
            activity,
            deposits=tuple(
                replace(d, gas_price=None)
                for d in activity.deposits
                if d.address_id != SUBJECT
            ),
            interactions=(),
        )
        anchor = replace(self.ANCHOR, gas_price=None)
        activity = replace(
            activity, withdrawals=(anchor, self.OWN_SECOND_LEG)
        )
        result = trace_back_from_withdrawal(anchor, activity)
        assert result.rung == MULTI_DENOMINATION_HEURISTIC
        assert hashes(result) == ["0xfinger-100"]

    def test_exactly_one_rung_produces_every_candidate_in_a_result(self) -> None:
        result = trace_back_from_withdrawal(self.ANCHOR, self._everything_fires())
        assert {c.heuristic for c in result.candidates} == {result.rung}

    def test_every_rung_id_is_a_versioned_string_in_ladder_order(self) -> None:
        assert MIXER_EXIT_LADDER == (
            ADDRESS_MATCH_HEURISTIC,
            LINKED_ADDRESS_HEURISTIC,
            GAS_PRICE_HEURISTIC,
            MULTI_DENOMINATION_HEURISTIC,
            ANONYMITY_SET_HEURISTIC,
        )
        assert all(rung.startswith("mixer-exit-") and "@" in rung for rung in MIXER_EXIT_LADDER)


# ── the safety property ───────────────────────────────────────────────────


class TestWeaknessIsMandatory:
    """A candidate with no stated weakness is indistinguishable in a report
    from a signature-verified hop. It has to be unconstructable."""

    def _valid_kwargs(self) -> dict[str, object]:
        return {
            "heuristic": ADDRESS_MATCH_HEURISTIC,
            "direction": Direction.BACKWARD,
            "address_id": 5,
            "tx_hash": "0xdep",
            "pool": POOL,
            "timestamp": T0,
            "value": 100,
            "confidence": 0.5,
        }

    def test_a_candidate_cannot_be_built_without_a_weakness_at_all(self) -> None:
        with pytest.raises(TypeError):
            MixerCandidate(**self._valid_kwargs())  # type: ignore[arg-type]

    def test_a_blank_weakness_is_refused(self) -> None:
        with pytest.raises(ValueError, match="weakness"):
            MixerCandidate(**self._valid_kwargs(), weakness="   ")  # type: ignore[arg-type]

    def test_a_candidate_may_not_claim_more_than_the_mixer_ceiling(self) -> None:
        with pytest.raises(ValueError, match="never a certainty"):
            MixerCandidate(
                **self._valid_kwargs() | {"confidence": 0.95},  # type: ignore[arg-type]
                weakness="a mixer crossing is never certain",
            )

    def test_a_candidate_may_not_claim_zero_confidence(self) -> None:
        with pytest.raises(ValueError, match="never a certainty"):
            MixerCandidate(
                **self._valid_kwargs() | {"confidence": 0.0},  # type: ignore[arg-type]
                weakness="a mixer crossing is never certain",
            )

    def test_a_candidate_may_not_carry_a_negative_value(self) -> None:
        """A negative denomination is a parsing failure upstream, not a very
        small transfer; carrying it through would rank it last and hide it."""
        with pytest.raises(ValueError, match="negative value"):
            MixerCandidate(
                **self._valid_kwargs() | {"value": -1},  # type: ignore[arg-type]
                weakness="a mixer crossing is never certain",
            )

    def test_a_candidate_must_name_a_versioned_heuristic(self) -> None:
        with pytest.raises(ValueError, match="name@version"):
            MixerCandidate(
                **self._valid_kwargs() | {"heuristic": "address-match"},  # type: ignore[arg-type]
                weakness="a mixer crossing is never certain",
            )

    def test_a_result_cannot_be_silent_about_what_happened(self) -> None:
        with pytest.raises(ValueError, match="including a refusal"):
            MixerExitResult(
                direction=Direction.BACKWARD,
                anchor_tx_hash="0xanchor",
                pool=POOL,
                rung=None,
                candidates=(),
                anonymity_set=0,
                capped=False,
                observation="",
            )

    @pytest.mark.parametrize(
        ("label", "activity"),
        [
            (
                "address match",
                MixerActivity(
                    deposits=(dep("0xmine", SUBJECT, at(-30)),),
                    withdrawals=(wd("0xanchor", SUBJECT, T0),),
                ),
            ),
            (
                "linked address",
                MixerActivity(
                    deposits=(dep("0xfriend", 5, at(-30)),),
                    withdrawals=(wd("0xanchor", SUBJECT, T0),),
                    interactions=(DirectInteraction("0xpay", 5, SUBJECT),),
                ),
            ),
            (
                "unique gas price",
                MixerActivity(
                    deposits=(dep("0xgas", 5, at(-30), gas_price=31_337_000_000),),
                    withdrawals=(wd("0xanchor", SUBJECT, T0, gas_price=31_337_000_000),),
                ),
            ),
            (
                "multi denomination",
                MixerActivity(
                    deposits=(
                        dep("0xf-100", 5, at(-30)),
                        dep("0xf-1", 5, at(-29), pool=OTHER_POOL, denomination=1),
                    ),
                    withdrawals=(
                        wd("0xanchor", SUBJECT, T0),
                        wd("0xanchor-2", SUBJECT, at(1), pool=OTHER_POOL, denomination=1),
                    ),
                ),
            ),
            (
                "anonymity set",
                MixerActivity(
                    deposits=(dep("0xcrowd", 5, at(-30)),),
                    withdrawals=(wd("0xanchor", SUBJECT, T0),),
                ),
            ),
        ],
    )
    def test_every_rung_hands_back_a_readable_weakness(
        self, label: str, activity: MixerActivity
    ) -> None:
        anchor = next(w for w in activity.withdrawals if w.tx_hash == "0xanchor")
        result = trace_back_from_withdrawal(anchor, activity)
        assert result.candidates, f"{label} produced nothing to check"
        for candidate in result.candidates:
            assert candidate.weakness.strip()
            assert " " in candidate.weakness.strip(), "a weakness is a sentence, not a code"
            assert 0.0 < candidate.confidence <= MAX_MIXER_CONFIDENCE

    def test_the_anonymity_set_weakness_reads_as_the_rfc_writes_it(self) -> None:
        """The example the RFC gives, verbatim in spirit: a count, and the
        phrase that stops a lead being read as an attribution."""
        anchor = wd("0xanchor", SUBJECT, T0)
        activity = MixerActivity(
            deposits=tuple(dep(f"0x{i:03d}", 10 + i, at(-i - 1)) for i in range(6)),
            withdrawals=(anchor,),
        )
        result = trace_back_from_withdrawal(anchor, activity)
        weakness = result.candidates[0].weakness
        assert "one of 6 deposits in the anonymity set" in weakness
        assert "this is a lead, not an attribution" in weakness

    def test_the_forward_anonymity_set_weakness_counts_withdrawals(self) -> None:
        """Naming the wrong side of the pool in the caveat would mislead a
        reader about what was actually enumerated."""
        anchor = dep("0xanchor", SUBJECT, T0)
        activity = MixerActivity(
            deposits=(anchor,),
            withdrawals=tuple(wd(f"0x{i:03d}", 10 + i, at(i + 1)) for i in range(6)),
        )
        result = trace_forward_from_deposit(anchor, activity)
        assert "one of 6 withdrawals in the anonymity set" in result.candidates[0].weakness


# ── inputs and constants ──────────────────────────────────────────────────


class TestWindowAndInputs:
    ANCHOR = wd("0xanchor", SUBJECT, T0)

    def test_the_documented_window_is_seven_days(self) -> None:
        """Δ is a guess (RFC §9) and a named constant so replacing it is one
        line. Pinned here so a silent change to it is visible in review."""
        assert MIXER_WINDOW.days == 7
        assert MIXER_WINDOW.seconds == 0 and MIXER_WINDOW.microseconds == 0

    def test_the_window_is_overridable_per_call(self) -> None:
        activity = MixerActivity(
            deposits=(dep("0xrecent", 5, at(-1)), dep("0xolder", 6, at(days=-3))),
            withdrawals=(self.ANCHOR,),
        )
        narrow = trace_back_from_withdrawal(self.ANCHOR, activity, window=timedelta(hours=2))
        assert hashes(narrow) == ["0xrecent"]
        wide = trace_back_from_withdrawal(self.ANCHOR, activity, window=timedelta(days=7))
        assert sorted(hashes(wide)) == ["0xolder", "0xrecent"]

    def test_the_window_is_named_in_the_weakness_so_a_reader_can_judge_it(self) -> None:
        activity = MixerActivity(deposits=(dep("0xa", 5, at(-1)),), withdrawals=(self.ANCHOR,))
        result = trace_back_from_withdrawal(self.ANCHOR, activity)
        assert "7 days" in result.candidates[0].weakness

    def test_a_non_positive_window_is_refused(self) -> None:
        with pytest.raises(ValueError, match="positive duration"):
            trace_back_from_withdrawal(self.ANCHOR, MixerActivity(), window=timedelta(0))

    def test_a_cap_below_one_is_refused(self) -> None:
        with pytest.raises(ValueError, match="at least one candidate"):
            trace_back_from_withdrawal(self.ANCHOR, MixerActivity(), max_follow=0)

    def test_a_mixer_event_must_name_its_pool(self) -> None:
        """Denomination is the whole lever (RFC §3); an event with no pool
        cannot be reasoned about at all."""
        with pytest.raises(ValueError, match="must name its pool"):
            MixerDeposit(
                tx_hash="0xa", address_id=1, pool="", denomination=100, timestamp=T0
            )

    def test_a_mixer_event_must_name_its_transaction(self) -> None:
        with pytest.raises(ValueError, match="name its transaction"):
            MixerWithdrawal(
                tx_hash="", address_id=1, pool=POOL, denomination=100, timestamp=T0
            )

    def test_a_mixer_event_may_not_carry_a_negative_denomination(self) -> None:
        with pytest.raises(ValueError, match="negative denomination"):
            MixerDeposit(
                tx_hash="0xa", address_id=1, pool=POOL, denomination=-1, timestamp=T0
            )

    def test_a_window_that_is_not_a_round_number_of_hours_is_still_named(self) -> None:
        """Δ is meant to be replaced with a measured value, which will not be
        a whole number of days. The weakness still has to state it, because a
        reader judging a lead needs to know how wide the net was."""
        activity = MixerActivity(deposits=(dep("0xa", 5, at(-1)),), withdrawals=(self.ANCHOR,))
        result = trace_back_from_withdrawal(
            self.ANCHOR, activity, window=timedelta(minutes=90)
        )
        assert result.candidates
        assert "1:30:00" in result.candidates[0].weakness

    def test_an_interaction_must_name_its_transaction(self) -> None:
        with pytest.raises(ValueError, match="name its transaction"):
            DirectInteraction(tx_hash="", from_address_id=1, to_address_id=2)

    def test_the_anchor_need_not_appear_in_the_activity_it_is_traced_against(self) -> None:
        """Callers assemble the view from a query and may or may not include
        the transaction being traced. The fingerprint must not depend on which
        of the two they did."""
        anchor = wd("0xanchor", SUBJECT, T0)
        second_leg = wd("0xanchor-2", SUBJECT, at(1), pool=OTHER_POOL, denomination=1)
        deposits = (
            dep("0xf-100", 5, at(-30)),
            dep("0xf-1", 5, at(-29), pool=OTHER_POOL, denomination=1),
        )
        with_anchor = MixerActivity(deposits=deposits, withdrawals=(anchor, second_leg))
        without_anchor = MixerActivity(deposits=deposits, withdrawals=(second_leg,))
        assert trace_back_from_withdrawal(anchor, with_anchor).rung == (
            trace_back_from_withdrawal(anchor, without_anchor).rung
        )
        assert hashes(trace_back_from_withdrawal(anchor, without_anchor)) == ["0xf-100"]

    def test_refs_are_sorted_and_deduplicated_for_reproducibility(self) -> None:
        """Two runs over the same facts must cite the same list in the same
        order, or a re-run of a case produces a diff in the report.

        The inputs are chosen so that raw insertion order is *not* already
        sorted and the same transaction is cited twice — one indexer row per
        direction of a transfer is an ordinary way for that to happen. An
        assertion against ``sorted(set(refs))`` alone cannot tell the two
        apart when the fixture hands over two already-ordered hashes."""
        anchor = wd("0xanchor", SUBJECT, T0)
        activity = MixerActivity(
            deposits=(dep("0xzz-deposit", 5, at(-30)),),
            withdrawals=(anchor,),
            interactions=(
                DirectInteraction("0xaa-payment", 5, SUBJECT),
                DirectInteraction("0xaa-payment", SUBJECT, 5),
            ),
        )
        result = trace_back_from_withdrawal(anchor, activity)
        assert result.rung == LINKED_ADDRESS_HEURISTIC
        # Insertion order would be anchor, deposit, payment, payment.
        assert result.candidates[0].refs == ("0xaa-payment", "0xanchor", "0xzz-deposit")
