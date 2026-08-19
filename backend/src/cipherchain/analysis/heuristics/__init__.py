"""Deterministic detectors over stored, normalized movements.

Every detector is versioned (``name@version``) and every conclusion it
emits is a HEURISTIC_INFERENCE carrying that version and a confidence —
never a fact, never a claim of identity (vision §4).
"""

from cipherchain.analysis.heuristics.obfuscation import (
    ALL_DETECTORS as OBFUSCATION_DETECTORS,
)
from cipherchain.analysis.heuristics.obfuscation import (
    detect_distribution,
    detect_fan_in,
    detect_peel_chain,
    detect_rapid_hop,
)
from cipherchain.analysis.heuristics.service import SERVICE_HEURISTIC, detect_service_endpoint
from cipherchain.analysis.heuristics.sweep import SWEEP_HEURISTIC, detect_sweeps

# Everything the engine runs on each expanded address.
ALL_DETECTORS = (detect_sweeps, *OBFUSCATION_DETECTORS)

__all__ = [
    "ALL_DETECTORS",
    "OBFUSCATION_DETECTORS",
    "SERVICE_HEURISTIC",
    "SWEEP_HEURISTIC",
    "detect_distribution",
    "detect_fan_in",
    "detect_peel_chain",
    "detect_rapid_hop",
    "detect_service_endpoint",
    "detect_sweeps",
]
