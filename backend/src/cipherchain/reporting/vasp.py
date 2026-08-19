"""Reaching VASP reference data without depending on it existing.

``cipherchain.intel.vasp_metadata`` is owned elsewhere and may be absent from a given
build entirely. The report must not care. A jurisdiction and a law-enforcement
request channel make a conclusion *actionable*, but a report that raises — or,
worse, 500s the endpoint that renders it — because a reference table has not
shipped yet would trade a whole document for a missing footnote.

So every failure mode here has the same outcome: the metadata is ABSENT and the
report says so. Module missing, attribute missing, signature different, row
missing, row malformed — all of them land on "not on file", which is a true
statement in every one of those cases.

Two deliberate shapes:

- **Coercion is duck-typed, resolution is not.** ``coerce_profile`` accepts any
  object carrying the schema-contract field names, or a mapping with those keys,
  because that contract is frozen and shared while the class wrapping it is not.
  The lookup callable, by contrast, is a narrow keyword-only protocol: the
  wiring layer that owns the metadata module passes one in explicitly.
- **The probe is a convenience, not the interface.** ``default_vasp_lookup``
  guesses at a few conventional names so a build that has the module gets
  metadata for free. When it guesses wrong the caller supplies ``vasp_lookup``
  and the probe is never consulted — which is why guessing wrong is cheap.
"""

from __future__ import annotations

import contextlib
import importlib
import inspect
import logging
from collections.abc import Mapping, Sequence
from datetime import date, datetime
from typing import Any, Protocol

from cipherchain.reporting.model import VaspProfile

logger = logging.getLogger(__name__)

METADATA_MODULE = "cipherchain.intel.vasp_metadata"

# Module-level callables the probe will accept, most specific first. A build
# that wants reporting to find its metadata without wiring can expose any one
# of them taking (chain, address, entity) keywords.
_LOOKUP_NAMES = ("report_lookup", "lookup_vasp_metadata", "vasp_metadata_for", "lookup")

# Repository classes the probe will accept, constructed with the session and
# asked for one entity. Matches the repository convention used elsewhere in
# storage (LabelRepository, InvestigationRepository).
_REPOSITORY_NAMES = ("VaspMetadataRepository", "VaspMetadata")
_REPOSITORY_METHODS = ("for_entity", "get_entity", "get")

# The frozen schema contract for the vasp_metadata table. Field names are read
# off whatever object the lookup returns, so an implementation is free to wrap
# them in any class it likes.
_FIELDS = (
    "jurisdiction",
    "legal_entity",
    "kyc_regime",
    "kyc_since",
    "le_request_channel",
    "source",
    "source_date",
)


class VaspLookup(Protocol):
    """Resolve reference data for one endpoint.

    Keyword-only and given all three keys — chain, address and the operator name
    read off the third-party claim — because an implementation may reasonably
    index by either address or entity, and the report cannot know which. Returning
    ``None`` (or anything unusable) means "not on file"; the result may be
    awaitable, since the real implementation reads a table.
    """

    def __call__(self, *, chain: str, address: str, entity: str | None) -> Any: ...


def _pick(raw: Any, name: str) -> Any:
    if isinstance(raw, Mapping):
        return raw.get(name)
    return getattr(raw, name, None)


def _text(value: Any) -> str | None:
    if value is None:
        return None
    if isinstance(value, str):
        stripped = value.strip()
        return stripped or None
    return str(value)


def _moment(value: Any) -> date | datetime | None:
    """Dates only. A string date is left alone rather than parsed and guessed at."""
    return value if isinstance(value, date | datetime) else None


def coerce_profile(raw: Any, *, entity: str | None = None) -> VaspProfile | None:
    """Read a VASP profile off whatever the lookup returned, or give up quietly.

    Gives up when there is no entity to name and nothing on file worth printing:
    a profile of blanks is worse than absence, because a table of empty cells
    reads as "checked and found nothing" when the truth is "nothing was checked".
    """
    if raw is None:
        return None
    name = _text(_pick(raw, "entity")) or _text(entity)
    if name is None:
        return None
    profile = VaspProfile(
        entity=name,
        jurisdiction=_text(_pick(raw, "jurisdiction")),
        legal_entity=_text(_pick(raw, "legal_entity")),
        kyc_regime=_text(_pick(raw, "kyc_regime")),
        kyc_since=_moment(_pick(raw, "kyc_since")),
        le_request_channel=_text(_pick(raw, "le_request_channel")),
        source=_text(_pick(raw, "source")),
        source_date=_moment(_pick(raw, "source_date")),
    )
    return profile if profile.has_content else None


async def resolve_profile(
    lookup: VaspLookup | None, *, chain: str, address: str, entity: str | None
) -> VaspProfile | None:
    """Call a lookup and survive it however it behaves.

    Awaits an awaitable result, tolerates a synchronous one, and swallows every
    exception into "not on file" — a reference-data lookup that raises must
    degrade a footnote, never the document. The failure is logged so it is
    fixable rather than invisible.
    """
    if lookup is None:
        return None
    try:
        result = lookup(chain=chain, address=address, entity=entity)
        if inspect.isawaitable(result):
            result = await result
        return coerce_profile(result, entity=entity)
    except Exception:
        logger.warning("VASP metadata lookup failed for %s on %s", address, chain, exc_info=True)
        return None


async def resolve_profiles(
    lookup: VaspLookup | None, endpoints: Sequence[tuple[str, str, str | None]]
) -> dict[str, VaspProfile]:
    """Profiles for the endpoints a third-party claim named, keyed by address.

    Keyed by address rather than entity because that is what the report holds
    for every endpoint. An endpoint with no claim entity is not looked up at
    all: metadata describes an OPERATOR, an unnamed endpoint has none, and an
    address-indexed lookup answering for one anyway would hand a legal entity
    and a filing channel to a card whose own badge reads "operator not named".
    ``cipherchain.intel.vasp_metadata`` states the same rule from the other side —
    nothing in that module can create a name, and nothing here may either.

    The refusal is repeated at attachment time (``AnswerEndpoint.of``), because
    this one only governs the lookups the report itself issues.
    """
    profiles: dict[str, VaspProfile] = {}
    for chain, address, entity in endpoints:
        if entity is None or address in profiles:
            continue
        profile = await resolve_profile(lookup, chain=chain, address=address, entity=entity)
        if profile is not None:
            profiles[address] = profile
    return profiles


def default_vasp_lookup(session: Any = None) -> VaspLookup | None:
    """Bind the intel package's VASP metadata reader, if this build ships one.

    Returns None — meaning "no metadata anywhere in this report" — when the
    module is absent or exposes nothing this probe recognises. That is not a
    failure state: the report renders identically minus the reference rows, and
    a caller that knows better passes its own lookup instead.
    """
    try:
        module = importlib.import_module(METADATA_MODULE)
    except Exception:  # ImportError, but a broken module must not break reports
        logger.debug("%s is not available; VASP metadata will be reported absent", METADATA_MODULE)
        return None

    for name in _LOOKUP_NAMES:
        candidate = getattr(module, name, None)
        if callable(candidate):
            return _bind_callable(candidate, session)

    for name in _REPOSITORY_NAMES:
        repository_class = getattr(module, name, None)
        if repository_class is None or not callable(repository_class):
            continue
        bound = _bind_repository(repository_class, session)
        if bound is not None:
            return bound

    logger.info("%s exposes no lookup this report recognises", METADATA_MODULE)
    return None


def _bind_callable(candidate: Any, session: Any) -> VaspLookup:
    """Adapt a module-level lookup, passing a session only if it wants one."""
    wants_session = False
    # Builtins and C callables have no introspectable signature; the report can
    # still call them, it just cannot offer them a session.
    with contextlib.suppress(TypeError, ValueError):
        wants_session = "session" in inspect.signature(candidate).parameters

    def lookup(*, chain: str, address: str, entity: str | None) -> Any:
        if wants_session:
            return candidate(session=session, chain=chain, address=address, entity=entity)
        return candidate(chain=chain, address=address, entity=entity)

    return lookup


def _bind_repository(repository_class: Any, session: Any) -> VaspLookup | None:
    """Adapt a repository class, if one of its methods answers by entity.

    Entity-keyed: a metadata table is per-operator, so an address-keyed call
    would have to resolve the label itself, which is the intel package's job and
    not something to reimplement here on a guess.
    """
    method_name = next(
        (name for name in _REPOSITORY_METHODS if callable(getattr(repository_class, name, None))),
        None,
    )
    if method_name is None:
        return None

    def lookup(*, chain: str, address: str, entity: str | None) -> Any:
        if entity is None:
            return None
        repository = repository_class(session)
        return getattr(repository, method_name)(entity)

    return lookup
