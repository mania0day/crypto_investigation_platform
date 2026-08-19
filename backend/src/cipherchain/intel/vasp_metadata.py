"""Who to actually serve — the metadata that turns a name into a respondent.

A trace that ends at "Binance" has answered the engine's question and not the
investigator's. A regulator cannot file against a string: filing needs a
legal person, a jurisdiction whose authorities can compel it, and a channel
that accepts the request. This module holds those facts for the operators our
labels actually name, and answers one question — *given this entity name,
what do I know about the company behind it?*

Three rules shape everything here.

**Absence is an answer.** ``lookup`` returns ``None`` for an unknown entity
and every field is nullable for a known one. There is no default row and no
"unknown jurisdiction" placeholder that could be mistaken for a finding. A
guessed jurisdiction sends process to the wrong country and burns weeks that
the money does not wait through, so a null here is not a gap in the data —
it is the datum: *this has not been established*. Rows whose fields are all
null still earn their place, because they say so out loud in ``notes``
instead of leaving the investigator to discover it at drafting time.

**Metadata never names anybody.** It only describes an operator some
``third_party_claim`` already named. Attaching this to a heuristic inference
would launder a "these addresses share a controller" into "this controller is
Binance Holdings Limited, serve them in the Cayman Islands" — the exact blur
the evidence taxonomy exists to prevent. Callers join on an entity a label
gave them; nothing in this module can create one.

**The join key is the entity STEM, not the raw string.** Real label entities
carry our own role annotations — ``Binance (deposit address)``,
``OKX (operational address)`` — which describe an address, not a company.
``policy.entity_stem`` is the canonicalization the lifecycle already uses to
decide whether two labels name one operator, so metadata uses it too rather
than inventing a second, subtly different notion of "same company". The data
file is validated against that: duplicate stems are refused (two rows for one
operator would resolve by file order, which is not a decision), and the test
suite refuses a row whose stem matches no shipped label — dead weight in a
file whose whole value is that every row is checkable.

The data lives in ``labels/metadata/`` rather than ``labels/`` because
``labels/*.json`` is globbed by the labelpack loader and the label import
script, both of which refuse a file that is not a labelpack. One directory
down keeps a non-labelpack out of a labelpack glob.

Two consumers, one contract. ``reporting.vasp`` probes this module for a
callable and calls it with ``(chain, address, entity)`` keywords, so
``report_lookup`` exists with exactly that signature — and the by-entity
convenience is called ``metadata_for``, NOT ``lookup``, because the probe
takes the first name it recognises and a same-named function with a
different signature would bind, raise, and be swallowed into "no metadata on
file" for every endpoint in every report. The ``vasp_metadata`` table is the
other consumer: ``scripts/import_vasp_metadata.py`` loads this file into it
for anyone joining in SQL, keyed by stem, as that table's contract requires.
"""

from __future__ import annotations

import json
from collections.abc import Iterator
from dataclasses import dataclass, field
from datetime import date
from functools import lru_cache
from pathlib import Path
from typing import Any, Final

from cipherchain.core.errors import ConfigurationError
from cipherchain.intel.policy import entity_stem

# backend/src/cipherchain/intel/vasp_metadata.py -> repo root -> labels/metadata/
DEFAULT_METADATA_PATH: Final = (
    Path(__file__).resolve().parents[4] / "labels" / "metadata" / "vasp-metadata.json"
)

# Every key must be PRESENT on every row, including the ones the author is
# setting to null. Omission and null read identically once loaded, and they
# are not the same act: null is a decision that this is not established,
# omission is a field nobody considered. Requiring the key forces the
# decision, and makes a typo ("jurisdication") a load error instead of a
# silently absent jurisdiction.
_REQUIRED_FIELDS: Final = (
    "entity",
    "jurisdiction",
    "legal_entity",
    "kyc_regime",
    "kyc_since",
    "le_request_channel",
    "source",
    "notes",
)
_OPTIONAL_FIELDS: Final = ("source_date",)

# Nulls that decide where process goes. Each one must be explained in
# ``notes`` — the file's contract is "where we are not confident, say so",
# and an unexplained null is indistinguishable from an unfinished row.
# ``kyc_since`` is deliberately not in this set: a missing date is
# unremarkable, while a missing forum is a decision.
_NULLS_NEEDING_EXPLANATION: Final = (
    "jurisdiction",
    "legal_entity",
    "kyc_regime",
    "le_request_channel",
)


@dataclass(frozen=True, slots=True)
class VaspMetadata:
    """One operator's filing facts. Every field but identity may be null.

    ``entity`` is the canonical operator name for display; ``stem`` is what
    lookups match on. ``source``/``source_date`` are mandatory for the same
    reason a labelpack cannot load without them: this is a claim about a
    company, and a claim nobody can date or trace is not evidence of
    anything.

    ``notes`` is FILE-ONLY: the table's frozen contract has no column for it
    and this module does not invent one. It carries why a field is null, for
    a human editing the file. Anything a *report* must show — "self-declared,
    not verified against a registry", "several group companies, establish
    which holds the account" — belongs in ``kyc_regime``,
    ``le_request_channel`` or ``source``, which do travel.
    """

    entity: str
    jurisdiction: str | None
    legal_entity: str | None
    kyc_regime: str | None
    kyc_since: date | None
    le_request_channel: str | None
    source: str
    source_date: date
    notes: str | None = None

    @property
    def stem(self) -> str:
        return entity_stem(self.entity)

    @property
    def is_serviceable(self) -> bool:
        """Does this row name both a respondent and a forum?

        The one question a drafting investigator asks. False does not mean
        the operator is unreachable — it means this file cannot say who to
        serve or where, and the work has to happen before the filing does.
        """
        return bool(self.jurisdiction and self.legal_entity)


@dataclass(frozen=True, slots=True)
class VaspMetadataIndex:
    """Stem-keyed metadata, built once and read many times.

    Holding the index as a value object (rather than a module-level dict)
    keeps the data injectable: tests and any future per-deployment override
    load their own file without touching process-wide state.
    """

    entries: tuple[VaspMetadata, ...]
    _by_stem: dict[str, VaspMetadata] = field(init=False, repr=False, compare=False)

    def __post_init__(self) -> None:
        object.__setattr__(self, "_by_stem", {entry.stem: entry for entry in self.entries})

    def lookup(self, entity: str) -> VaspMetadata | None:
        """Metadata for ``entity``, or ``None`` — never an exception, never a
        placeholder row.

        Matching goes through ``entity_stem``, so the annotated strings that
        actually appear in labels resolve: ``"Binance (deposit address)"``,
        ``"Binance 14"`` and ``"binance"`` all reach the Binance row. An
        entity that stems to nothing ("", "(deposit)") names no operator and
        matches nothing — the index never holds an empty stem, so the lookup
        cannot accidentally hit one.
        """
        return self._by_stem.get(entity_stem(entity))

    def __iter__(self) -> Iterator[VaspMetadata]:
        return iter(self.entries)

    def __len__(self) -> int:
        return len(self.entries)


def _text_or_none(row: dict[str, Any], key: str, path: Path, entity: str) -> str | None:
    """A nullable text field. Empty string is refused, not coerced to null:
    ``""`` is a null wearing a disguise, and downstream truthiness checks
    would treat it as an answer.
    """
    raw = row[key]
    if raw is None:
        return None
    if not isinstance(raw, str) or not raw.strip():
        raise ConfigurationError(
            f"{path.name}: {entity!r} field {key!r} must be a non-empty string or null"
        )
    return raw.strip()


def _parse_date(raw: object, path: Path, entity: str, key: str) -> date | None:
    if raw is None:
        return None
    try:
        return date.fromisoformat(str(raw))
    except ValueError as exc:
        raise ConfigurationError(f"{path.name}: {entity!r} {key} {raw!r} is not a date") from exc


def _build_row(row: dict[str, Any], path: Path, file_source_date: date | None) -> VaspMetadata:
    unknown = sorted(set(row) - set(_REQUIRED_FIELDS) - set(_OPTIONAL_FIELDS))
    if unknown:
        raise ConfigurationError(f"{path.name}: unknown field(s) {', '.join(unknown)}")
    missing = [key for key in _REQUIRED_FIELDS if key not in row]
    if missing:
        raise ConfigurationError(
            f"{path.name}: row {row.get('entity', '?')!r} omits {', '.join(missing)} — "
            "state null explicitly rather than leaving a field unconsidered"
        )

    entity = str(row["entity"]).strip()
    if not entity:
        raise ConfigurationError(f"{path.name}: a metadata row must name an entity")
    if not entity_stem(entity):
        # All annotation, no name — it could never match a label, and worse,
        # an empty stem in the index would swallow every unnamed lookup.
        raise ConfigurationError(f"{path.name}: entity {entity!r} names no operator")

    notes = _text_or_none(row, "notes", path, entity)
    unexplained = [key for key in _NULLS_NEEDING_EXPLANATION if row[key] is None]
    if unexplained and not notes:
        raise ConfigurationError(
            f"{path.name}: {entity!r} leaves {', '.join(unexplained)} null with no notes — "
            "where a fact is not established the row must SAY so"
        )

    source = _text_or_none(row, "source", path, entity)
    if not source:
        raise ConfigurationError(f"{path.name}: {entity!r} has no source — provenance is required")

    source_date = _parse_date(row.get("source_date"), path, entity, "source_date")
    if source_date is None:
        source_date = file_source_date
    if source_date is None:
        raise ConfigurationError(
            f"{path.name}: {entity!r} has no source_date and the file declares no default — "
            "an undated claim about a company cannot be weighed"
        )

    return VaspMetadata(
        entity=entity,
        jurisdiction=_text_or_none(row, "jurisdiction", path, entity),
        legal_entity=_text_or_none(row, "legal_entity", path, entity),
        kyc_regime=_text_or_none(row, "kyc_regime", path, entity),
        kyc_since=_parse_date(row["kyc_since"], path, entity, "kyc_since"),
        le_request_channel=_text_or_none(row, "le_request_channel", path, entity),
        source=source,
        source_date=source_date,
        notes=notes,
    )


def load_vasp_metadata(path: Path = DEFAULT_METADATA_PATH) -> VaspMetadataIndex:
    """Load and validate one metadata file.

    Loudly on every malformed input. A file that half-loads is the worst
    outcome available: the rows that dropped out become "no metadata for this
    operator", which reads exactly like an honest null and would send an
    investigator to research facts that were sitting in the file all along.
    """
    try:
        raw: Any = json.loads(path.read_text())
    except (OSError, ValueError) as exc:
        raise ConfigurationError(f"cannot read VASP metadata {path}: {exc}") from exc
    if not isinstance(raw, dict):
        raise ConfigurationError(f"{path.name}: expected a JSON object at the top level")

    file_source_date = _parse_date(raw.get("source_date"), path, "<file>", "source_date")
    rows = raw.get("vasps")
    if not isinstance(rows, list):
        raise ConfigurationError(f"{path.name}: no 'vasps' list")

    entries: list[VaspMetadata] = []
    by_stem: dict[str, str] = {}
    for row in rows:
        if not isinstance(row, dict):
            raise ConfigurationError(f"{path.name}: every entry in 'vasps' must be an object")
        entry = _build_row(row, path, file_source_date)
        if entry.stem in by_stem:
            # Two rows for one operator resolve by file order, and order is
            # not a decision — the same failure the labelpack collision guard
            # exists for. Here it would be worse: the loser's jurisdiction
            # simply vanishes, silently.
            raise ConfigurationError(
                f"{path.name}: {entry.entity!r} and {by_stem[entry.stem]!r} name the same "
                f"operator ({entry.stem!r}) — one row per operator, merge them"
            )
        by_stem[entry.stem] = entry.entity
        entries.append(entry)
    return VaspMetadataIndex(entries=tuple(entries))


@lru_cache(maxsize=1)
def default_index() -> VaspMetadataIndex:
    """The shipped metadata file, parsed once per process.

    Cacheable only because everything it returns is frozen. A missing or
    broken file raises here rather than degrading to "no operator has
    metadata": that failure mode is invisible in output and would look like
    an honest set of nulls.
    """
    return load_vasp_metadata()


def metadata_for(entity: str) -> VaspMetadata | None:
    """Shipped-file convenience for callers that need no injection."""
    return default_index().lookup(entity)


def report_lookup(*, chain: str, address: str, entity: str | None) -> VaspMetadata | None:
    """The reporting layer's lookup protocol (``reporting.vasp.VaspLookup``).

    ``chain`` and ``address`` are accepted and ignored, deliberately: this
    file describes OPERATORS, and one operator's filing facts are identical
    for every address it controls. The report passes all three keys because
    it cannot know which an implementation indexes by; answering by entity
    alone is the honest reading of that protocol, not a shortcut.

    Answers from the FILE, not the table, so a report is correct on a build
    where the import script has never run. The table exists for consumers
    that join in SQL.
    """
    return None if entity is None else default_index().lookup(entity)
