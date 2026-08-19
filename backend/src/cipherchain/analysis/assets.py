"""Which assets may found a heuristic inference.

A token contract is free to emit whatever transfer events it likes, between
addresses that never signed anything. So an attacker can deploy a worthless
token, emit ``Transfer(random -> victim)`` then ``Transfer(victim -> exchange)``
a few times, and manufacture a complete receive-and-forward pattern against a
target of their choosing, for the price of gas. The victim does nothing and
consents to nothing.

Nothing downstream can undo that: the movements are real *events*, faithfully
normalized. The only defence is upstream — refuse to found an inference on an
asset whose provenance CipherChain has not established.

The rule:

- **Native assets always qualify.** Moving them requires the key.
- **Token assets qualify only if the contract is on the verified list**, read
  from the issuer's own documentation and confirmed on-chain before shipping
  (repo-root ``assets/``). Same posture as ``labels/`` and ``bridges/``:
  CipherChain ships nothing it has not verified.

This is deliberately a *floor*, not a filter on traversal. Unverified-asset
movements are still stored and still expand the graph — they are real events
and hiding them would be its own dishonesty. They simply cannot be the
evidence a heuristic points at.
"""

from __future__ import annotations

import json
import logging
from collections.abc import Iterable, Iterator
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from cipherchain.core.errors import ConfigurationError
from cipherchain.storage.repositories import AssetFacts

logger = logging.getLogger(__name__)

ASSET_POLICY_VERSION = "verified-assets@1"

DEFAULT_ASSETS_DIR = Path(__file__).resolve().parents[4] / "assets"


@dataclass(frozen=True, slots=True)
class VerifiedAsset:
    """One token contract whose provenance has been established."""

    chain: str
    contract: str
    symbol: str
    issuer: str
    source: str
    source_url: str | None = None


def _normalize_contract(chain: str, contract: str) -> str:
    """EVM contracts are case-insensitive hex; Tron/Solana are Base58 and are
    case-SIGNIFICANT, so folding them would break the lookup."""
    value = contract.strip()
    return value.lower() if value[:2].lower() == "0x" else value


class AssetPolicy:
    """Decides whether an asset is strong enough to carry an inference."""

    def __init__(self, verified: Iterable[VerifiedAsset] = ()) -> None:
        self._verified: dict[tuple[str, str], VerifiedAsset] = {}
        for asset in verified:
            self._verified[(asset.chain, _normalize_contract(asset.chain, asset.contract))] = asset

    def __len__(self) -> int:
        return len(self._verified)

    def is_evidence_grade(self, *, chain: str, kind: str, contract: str | None) -> bool:
        if kind == "native":
            return True
        if not contract:
            # A token movement with no contract cannot be checked against
            # anything, so it cannot be verified.
            return False
        return (chain, _normalize_contract(chain, contract)) in self._verified

    def accepts(self, asset: AssetFacts) -> bool:
        """The form the engine injects: judge a stored asset row."""
        return self.is_evidence_grade(chain=asset.chain, kind=asset.kind, contract=asset.contract)

    def lookup(self, chain: str, contract: str) -> VerifiedAsset | None:
        return self._verified.get((chain, _normalize_contract(chain, contract)))


def load_asset_pack(path: Path) -> list[VerifiedAsset]:
    """Load and validate one verified-asset pack."""
    try:
        raw: dict[str, Any] = json.loads(path.read_text())
    except (OSError, ValueError) as exc:
        raise ConfigurationError(f"cannot read asset pack {path}: {exc}") from exc
    source = raw.get("source")
    if not source:
        raise ConfigurationError(f"asset pack {path} has no 'source' — provenance is required")
    if not raw.get("source_date"):
        raise ConfigurationError(f"asset pack {path} has no 'source_date'")
    entries = raw.get("assets")
    if not isinstance(entries, list):
        raise ConfigurationError(f"asset pack {path} has no 'assets' list")
    verified: list[VerifiedAsset] = []
    for index, entry in enumerate(entries):
        try:
            verified.append(
                VerifiedAsset(
                    chain=str(entry["chain"]),
                    contract=str(entry["contract"]),
                    symbol=str(entry["symbol"]),
                    issuer=str(entry["issuer"]),
                    source=str(source),
                    source_url=(
                        str(entry["source_url"]) if entry.get("source_url") is not None else None
                    ),
                )
            )
        except (KeyError, TypeError) as exc:
            raise ConfigurationError(
                f"asset pack {path} entry {index} is malformed: {exc}"
            ) from exc
    return verified


def load_asset_dir(directory: Path) -> Iterator[VerifiedAsset]:
    if not directory.is_dir():
        return
    for path in sorted(directory.glob("*.json")):
        assets = load_asset_pack(path)
        logger.info("loaded asset pack %s (%d verified assets)", path.name, len(assets))
        yield from assets


def build_asset_policy(directory: Path | None = None) -> AssetPolicy:
    """The policy the engine hands to its heuristics.

    With no pack present the policy is native-only — which is safe, not broken:
    heuristics simply stand on the one asset class that cannot be forged.
    """
    target = directory if directory is not None else DEFAULT_ASSETS_DIR
    policy = AssetPolicy(load_asset_dir(target))
    if not len(policy):
        logger.warning(
            "no verified assets loaded from %s — heuristics will consider native assets only",
            target,
        )
    return policy
