"""Mixer exit analysis — the one place a guess is allowed to be produced.

Everything else in ``analysis`` describes what the ledger shows. This package
does something different and more dangerous: it *selects* a counterparty on
the far side of a deliberately severed link. Published linkage rates for
Tornado Cash put the realistic ceiling for all five heuristics stacked at
roughly one withdrawal in three (REACHING_THE_VASP.md §3), which means the
common case is that the selection is wrong.

That is acceptable only because every candidate leaves here carrying a
mandatory plain-language weakness, and because a mixer-exit candidate can
never become a name: naming an operator requires a ``third_party_claim``, and
nothing in this package produces one.

Import ``trace_back_from_withdrawal`` or ``trace_forward_from_deposit`` — the
direction is chosen by which function you call and which type you hand it, so
the two cannot be confused silently.
"""

from cipherchain.analysis.mixers.exits import (
    ADDRESS_MATCH_HEURISTIC,
    ANONYMITY_SET_HEURISTIC,
    ANONYMITY_SET_MAX_CONFIDENCE,
    GAS_PRICE_HEURISTIC,
    LINKED_ADDRESS_HEURISTIC,
    MAX_FOLLOW,
    MAX_MIXER_CONFIDENCE,
    MIN_FINGERPRINT_POOLS,
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

__all__ = [
    "ADDRESS_MATCH_HEURISTIC",
    "ANONYMITY_SET_HEURISTIC",
    "ANONYMITY_SET_MAX_CONFIDENCE",
    "GAS_PRICE_HEURISTIC",
    "LINKED_ADDRESS_HEURISTIC",
    "MAX_FOLLOW",
    "MAX_MIXER_CONFIDENCE",
    "MIN_FINGERPRINT_POOLS",
    "MIXER_EXIT_LADDER",
    "MIXER_WINDOW",
    "MULTI_DENOMINATION_HEURISTIC",
    "DirectInteraction",
    "MixerActivity",
    "MixerCandidate",
    "MixerDeposit",
    "MixerExitResult",
    "MixerWithdrawal",
    "trace_back_from_withdrawal",
    "trace_forward_from_deposit",
]
