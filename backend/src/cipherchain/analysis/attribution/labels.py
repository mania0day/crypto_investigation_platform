"""Label records and the labelpack file format.

A labelpack is one JSON file carrying its own provenance — nothing may be
loaded without a source and a date, because every label is a *claim*, not
ground truth (vision §4). The shape is deliberately close to community
tag-format conventions so open tagpacks can be converted mechanically.

    {
      "source": "my-org-vasp-labels",
      "source_date": "2026-08-07",
      "license": "CC0-1.0",
      "default_confidence": 0.8,
      "labels": [
        {"chain": "ethereum", "address": "0x…", "entity": "Acme Exchange",
         "category": "vasp", "confidence": 0.9}
      ]
    }

Addresses are normalized on load (0x-prefixed forms lowercased) so lookups
match the canonical addresses adapters produce.
"""

from __future__ import annotations

import json
import logging
from collections.abc import Iterable, Iterator
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Protocol

from cipherchain.core.errors import ConfigurationError
from cipherchain.investigation.attribution import (
    CATEGORY_INFRASTRUCTURE,
    CATEGORY_MIXER,
    CATEGORY_SANCTIONED,
    CATEGORY_VASP,
    AddressRole,
)


def normalize_address(value: str) -> str:
    """Canonical lookup form. Hex addresses are case-insensitive; Base58
    addresses are case-SIGNIFICANT and must be left alone.

    The ``0x`` prefix is itself matched case-insensitively, so an address
    typed ``0XABC…`` resolves to the same label as ``0xabc…``.
    """
    text = value.strip()
    return text.lower() if text[:2].lower() == "0x" else text


logger = logging.getLogger(__name__)

# The categories the engine actually acts on. A label outside this set loads
# fine and then does nothing, so loading must SAY so rather than stay quiet.
ACTIONABLE_CATEGORIES = frozenset(
    {CATEGORY_VASP, CATEGORY_SANCTIONED, CATEGORY_MIXER, CATEGORY_INFRASTRUCTURE}
)


@dataclass(frozen=True, slots=True)
class LabelRecord:
    chain: str
    address: str
    entity: str
    category: str
    source: str
    confidence: float
    source_date: datetime | None = None
    role: AddressRole = AddressRole.UNKNOWN

    def __post_init__(self) -> None:
        if not (self.chain and self.address and self.entity and self.category and self.source):
            raise ValueError("label requires chain, address, entity, category, and source")
        # STRICTLY below 1.0, matching Evidence: a third-party claim is never
        # certainty. Admitting 1.0 here only deferred the error to mid-run, where
        # Evidence rejects it while building the finding (core/models.py).
        if not (0.0 < self.confidence < 1.0):
            raise ValueError("label confidence must be in (0, 1) — a label is a claim, not truth")

    @property
    def key(self) -> tuple[str, str]:
        return (self.chain, normalize_address(self.address))


class LabelSource(Protocol):
    """A body of labels with provenance. Implementations load from files,
    vendored datasets, or (later) a database."""

    @property
    def name(self) -> str: ...

    def records(self) -> Iterable[LabelRecord]: ...


@dataclass(frozen=True, slots=True)
class LabelPack:
    """A file-backed label source."""

    name: str
    labels: tuple[LabelRecord, ...]

    def records(self) -> Iterable[LabelRecord]:
        return self.labels


def _parse_date(raw: str | None) -> datetime | None:
    if not raw:
        return None
    try:
        parsed = datetime.fromisoformat(raw)
    except ValueError as exc:
        raise ConfigurationError(f"invalid source_date {raw!r}") from exc
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=UTC)


def _parse_role(raw: object) -> AddressRole:
    """Unrecognised or absent role reads as UNKNOWN, never as a guess.

    A pack that does not declare what kind of address it is describing has not
    said "operational"; treating silence as either specific role would invent a
    distinction the operator never asserted.
    """
    if raw is None:
        return AddressRole.UNKNOWN
    try:
        return AddressRole(str(raw))
    except ValueError:
        return AddressRole.UNKNOWN


def load_labelpack(path: Path) -> LabelPack:
    """Load and validate one labelpack file."""
    try:
        raw: dict[str, Any] = json.loads(path.read_text())
    except (OSError, ValueError) as exc:
        raise ConfigurationError(f"cannot read labelpack {path}: {exc}") from exc
    source = raw.get("source")
    if not source:
        raise ConfigurationError(f"labelpack {path} has no 'source' — provenance is required")
    # Ruling 3: operator-supplied labels get no free pass. An undated claim would
    # reach an evidence record undated, and a reader cannot weigh a label without
    # knowing how old it is. Refuse data that would make a RECORDED CLAIM
    # unprovenanced; be loud but permissive about data that merely goes unused.
    if not raw.get("source_date"):
        raise ConfigurationError(
            f"labelpack {path} has no 'source_date' — a claim without a date "
            "cannot be weighed, and every label is a claim"
        )
    source_date = _parse_date(raw.get("source_date"))
    default_confidence = float(raw.get("default_confidence", 0.7))
    entries = raw.get("labels")
    if not isinstance(entries, list):
        raise ConfigurationError(f"labelpack {path} has no 'labels' list")
    records: list[LabelRecord] = []
    inert: dict[str, int] = {}
    for index, entry in enumerate(entries):
        try:
            records.append(
                LabelRecord(
                    chain=str(entry["chain"]),
                    address=str(entry["address"]),
                    entity=str(entry["entity"]),
                    category=str(entry["category"]),
                    source=str(source),
                    confidence=float(entry.get("confidence", default_confidence)),
                    source_date=source_date,
                    role=_parse_role(entry.get("role")),
                )
            )
        except (KeyError, TypeError, ValueError) as exc:
            raise ConfigurationError(f"labelpack {path} entry {index}: {exc}") from exc
        category = records[-1].category
        if category not in ACTIONABLE_CATEGORIES:
            inert[category] = inert.get(category, 0) + 1
    if inert:
        # Loaded, counted, and acted on by nothing. Silence here is the worst
        # outcome: a pack written "exchange" instead of "vasp" loads cleanly,
        # logs a healthy count, and matches nothing — so the tool reports "no
        # VASP found" for addresses the operator believes it labelled. Never
        # aliased to a known category either: guessing exchange -> vasp would
        # manufacture attributions the operator never asserted.
        logger.warning(
            "labelpack %s: %d label(s) use categories nothing acts on (%s) — "
            "they are loaded but INERT; actionable categories are %s",
            path.name,
            sum(inert.values()),
            ", ".join(f"{name} x{count}" for name, count in sorted(inert.items())),
            ", ".join(sorted(ACTIONABLE_CATEGORIES)),
        )
    return LabelPack(name=str(source), labels=tuple(records))


def load_labelpack_dir(directory: Path) -> Iterator[LabelPack]:
    """Load every ``*.json`` labelpack in a directory (sorted, stable)."""
    if not directory.is_dir():
        return
    for path in sorted(directory.glob("*.json")):
        yield load_labelpack(path)
