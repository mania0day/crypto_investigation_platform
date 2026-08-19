"""Lifecycle policy — every rule pure, every rule stated (LABEL_INTELLIGENCE.md §4).

This module decides; it never touches storage or the web. The service applies
these decisions over the repository, so the policy is testable as plain
functions and auditable by reading one file.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import datetime

from cipherchain.storage.repositories import StoredLabel

# The three harvest tiers ruling 2 admits. A claim verified by any of them
# activates ON ARRIVAL: the source restriction is the vetting, so what
# survives it may attribute. Community is the deliberate exception — a report
# is intel, not evidence, until something in this set agrees with it.
TRUSTED_METHODS = frozenset({"signature", "first_party_published", "licensed_dataset"})


def arrival_status(method: str) -> str:
    return "active" if method in TRUSTED_METHODS else "pending"


@dataclass(frozen=True, slots=True)
class IntelClaim:
    """One claim as it arrives — from a harvest source or a community report.

    Untrusted claims get a stricter entity: a NAME, not prose. Stem matching
    ignores parentheticals and trailing indexes — they are annotation syntax
    in OUR curated packs — so in a community entity they are a smuggling
    channel: "Binance (successor wallet 0xATTACKER)" stems to "binance",
    promotes against real Binance data, and the verbatim prose becomes an
    active, citable label. Review demonstrated exactly that. So for any
    method that arrives pending, the entity must be short, single-line, and
    carry no parentheses and no URLs; role is its own field.
    """

    chain: str
    address: str
    entity: str
    category: str
    role: str
    confidence: float
    method: str
    source: str
    retrieved_at: datetime
    source_date: datetime | None = None
    evidence_url: str | None = None
    reporter: str | None = None

    def __post_init__(self) -> None:
        if arrival_status(self.method) != "pending":
            return
        entity = self.entity.strip()
        problem = (
            "empty"
            if not entity
            else "too long (max 64)"
            if len(entity) > 64
            else "must be a single line"
            if any(c in entity for c in "\r\n")
            else "annotation syntax is reserved for curated packs"
            if "(" in entity or ")" in entity
            else "must not contain a URL"
            if "://" in entity
            else None
        )
        if problem is not None:
            raise ValueError(f"untrusted claim entity {problem}: {entity[:80]!r}")


# "Binance 14", "Binance (operational address)", "binance" all name one
# operator; the wallet index and the role suffix are OUR annotations, not
# part of who is being named. The trailing-index strip REQUIRES a separator:
# "Binance 14" sheds its wallet number, but "Aave V2" keeps its 2 — review
# showed the looser [\s\-_]* collapsed V2 and V3 into one stem.
_PARENTHETICAL = re.compile(r"\s*\([^)]*\)")
_TRAILING_INDEX = re.compile(r"[\s\-_]+\d+$")


def entity_stem(entity: str) -> str:
    """The comparable core of an entity name. Empty when nothing NAMES: an
    entity that is all annotation ("14", "(deposit)") identifies nobody, and
    the caller treats an empty stem as never matching."""
    stem = _PARENTHETICAL.sub("", entity)
    stem = _TRAILING_INDEX.sub("", stem)
    if not re.search(r"[^\d\s\-_]", stem):
        return ""
    return " ".join(stem.casefold().split())


def corroborates(claimant: StoredLabel, candidate: StoredLabel) -> bool:
    """May ``candidate`` justify ``claimant`` being active? The conservative
    side of every line, because the two errors are not symmetric: a false NO
    costs waiting, a false YES puts an unverified claim into the attributor —
    the exact failure the lifecycle exists to prevent.

    Used both to PROMOTE a pending claim and to RE-JUSTIFY an active one each
    cycle (service.reconcile): corroboration is a standing condition, not a
    stamp. Review proved the stamp version attackable — promote honest
    content, then edit the row into something nobody ever corroborated.

    - the **same chain and address**: evidence about one address says nothing
      about another. This is the caller's join too, but mutation testing
      proved the suite could not pin the caller, so the predicate holds its
      own ground.
    - candidate must be **active** and of a **trusted method**: pending
      agreeing with pending is not verification, and a promoted community
      report must never itself corroborate — chains of reports would let two
      sock-puppets hold each other active after their real basis retired.
      Trust flows one way: from harvested sources to reports, never between
      reports.
    - a **different source**: a source cannot corroborate itself.
    - the **same category**: an active sanctions listing does not confirm a
      report that called the address an exchange — it contradicts it.
    - **equal entity stems**: "Binance 14" confirms "Binance (deposit)";
      "Binance Charity" does NOT confirm "Binance" — different stem, plausibly
      a different operator, so it waits. Prefix or substring matching in
      either direction is exactly the shortcut this predicate refuses.
    """
    if candidate.chain != claimant.chain or candidate.address != claimant.address:
        return False
    if candidate.status != "active":
        return False
    if candidate.method not in TRUSTED_METHODS:
        return False
    if candidate.source == claimant.source:
        return False
    if candidate.category != claimant.category:
        return False
    stem = entity_stem(claimant.entity)
    return bool(stem) and stem == entity_stem(candidate.entity)
