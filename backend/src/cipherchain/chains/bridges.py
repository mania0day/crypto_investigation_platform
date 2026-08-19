"""Bridge contract registry — chain-local knowledge, part of the Chain SDK.

Which contract on *this* chain is a bridge is chain-local knowledge, so it
belongs beside the adapters rather than in the engine or in analysis. An
adapter consults the registry during ``normalize()`` and emits a
:class:`BridgeHint` when a transaction touches a known bridge.

Matching a deposit on one chain to its payout on another is a *different*
problem — it needs data from two chains at once — and lives in
``analysis/bridges`` (frozen Chain SDK ruling D1).

Entries are operator-supplied data, loaded from JSON exactly like
labelpacks, because a bridge address asserted without provenance is the
same failure mode as an unsourced attribution: CipherChain would tell an
investigator that funds crossed a bridge on the strength of a guess.
"""

from __future__ import annotations

import json
import logging
from collections.abc import Iterable, Iterator
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from cipherchain.chains.base import BridgeDirection
from cipherchain.core.errors import ConfigurationError

logger = logging.getLogger(__name__)


def _lookup_key(address: str) -> str:
    """Case-fold hex addresses only.

    The ``0x`` prefix itself may be typed either case, so the check must be
    case-insensitive — otherwise ``0XABC…`` silently fails to match the same
    address written ``0xabc…``. Base58 addresses are case-SIGNIFICANT and
    must never be folded.
    """
    value = address.strip()
    return value.lower() if value[:2].lower() == "0x" else value


@dataclass(frozen=True, slots=True)
class BridgeEntry:
    """One bridge contract on one chain."""

    bridge_id: str
    name: str
    chain: str
    address: str
    direction: BridgeDirection
    counterpart_chain: str | None
    source: str
    source_date: datetime | None = None

    def __post_init__(self) -> None:
        if not (self.bridge_id and self.name and self.chain and self.address):
            raise ValueError("bridge entry requires bridge_id, name, chain and address")
        if not self.source:
            raise ValueError("bridge entry requires a source — provenance is mandatory")

    @property
    def key(self) -> str:
        """Lookup form. Hex addresses are case-insensitive; others are not."""
        return _lookup_key(self.address)


class BridgeRegistry:
    """Address → bridge lookup, scoped per chain."""

    def __init__(self, entries: Iterable[BridgeEntry] = ()) -> None:
        self._by_chain: dict[str, dict[str, BridgeEntry]] = {}
        self.sources: list[str] = []
        for entry in entries:
            self.add(entry)

    def add(self, entry: BridgeEntry) -> None:
        self._by_chain.setdefault(entry.chain, {})[entry.key] = entry
        if entry.source not in self.sources:
            self.sources.append(entry.source)

    def lookup(self, chain: str, address: str) -> BridgeEntry | None:
        return self._by_chain.get(chain, {}).get(_lookup_key(address))

    def for_chain(self, chain: str) -> BridgeRegistry:
        """A view containing only this chain's entries — what an adapter needs."""
        return BridgeRegistry(self._by_chain.get(chain, {}).values())

    def __len__(self) -> int:
        return sum(len(v) for v in self._by_chain.values())

    def chains(self) -> tuple[str, ...]:
        return tuple(sorted(self._by_chain))


def _parse_date(raw: str | None) -> datetime | None:
    if not raw:
        return None
    try:
        parsed = datetime.fromisoformat(raw)
    except ValueError as exc:
        raise ConfigurationError(f"invalid source_date {raw!r}") from exc
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=UTC)


def load_bridge_pack(path: Path) -> list[BridgeEntry]:
    """Load one bridge pack file. Format mirrors labelpacks."""
    try:
        raw: dict[str, Any] = json.loads(path.read_text())
    except (OSError, ValueError) as exc:
        raise ConfigurationError(f"cannot read bridge pack {path}: {exc}") from exc
    source = raw.get("source")
    if not source:
        raise ConfigurationError(f"bridge pack {path} has no 'source' — provenance is required")
    source_date = _parse_date(raw.get("source_date"))
    entries_raw = raw.get("bridges")
    if not isinstance(entries_raw, list):
        raise ConfigurationError(f"bridge pack {path} has no 'bridges' list")
    out: list[BridgeEntry] = []
    for index, item in enumerate(entries_raw):
        try:
            out.append(
                BridgeEntry(
                    bridge_id=str(item["bridge_id"]),
                    name=str(item["name"]),
                    chain=str(item["chain"]),
                    address=str(item["address"]),
                    direction=BridgeDirection(str(item.get("direction", "deposit"))),
                    counterpart_chain=(
                        str(item["counterpart_chain"]) if item.get("counterpart_chain") else None
                    ),
                    source=str(source),
                    source_date=source_date,
                )
            )
        except (KeyError, TypeError, ValueError) as exc:
            raise ConfigurationError(f"bridge pack {path} entry {index}: {exc}") from exc
    return out


def load_bridge_dir(directory: Path) -> Iterator[BridgeEntry]:
    """Load every ``*.json`` bridge pack in a directory (sorted, stable)."""
    if not directory.is_dir():
        return
    for path in sorted(directory.glob("*.json")):
        entries = load_bridge_pack(path)
        logger.info("loaded bridge pack %s (%d contracts)", path.name, len(entries))
        yield from entries


def build_bridge_registry(directory: Path | None = None) -> BridgeRegistry:
    """Assemble the registry from operator-supplied packs.

    Empty by default: CipherChain ships no bridge addresses it has not verified,
    for the same reason it ships no invented VASP labels.
    """
    registry = BridgeRegistry()
    if directory is not None:
        for entry in load_bridge_dir(directory):
            registry.add(entry)
    logger.info(
        "bridge registry: %d contract(s) across %s", len(registry), registry.chains() or "—"
    )
    return registry
