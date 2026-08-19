"""OFAC sanctioned digital-currency addresses as a label source.

Dataset: github.com/0xB10C/ofac-sanctioned-digital-currency-addresses
(MIT licensed — license verified before vendoring, per the vision's
license-clean requirement). Snapshots live in ``analysis/data`` and are
dated; refreshing is ``scripts/refresh_ofac.py``.

Every record becomes a third-party claim carrying the snapshot date, so a
finding always states *which* version of the list it rests on. Confidence
is high but not 1.0: the underlying attribution can be stale or contested,
and CipherChain never presents a claim as certainty.

Asset-list note: OFAC publishes per-asset lists. USDT addresses are
Ethereum addresses, and TRX addresses are Tron addresses — they are mapped
onto the chains where those addresses actually exist, not treated as
separate chains.
"""

from __future__ import annotations

import json
import logging
from collections.abc import Iterable, Mapping
from datetime import UTC, datetime
from pathlib import Path

from cipherchain.analysis.attribution.labels import LabelRecord

logger = logging.getLogger(__name__)

DATA_DIR = Path(__file__).resolve().parent.parent / "data"
OFAC_SNAPSHOT_DATE = datetime(2026, 8, 7, tzinfo=UTC)
SOURCE_NAME = "ofac-sdn-digital-currency@2026-08-07"
CATEGORY = "sanctioned"
CONFIDENCE = 0.95

# vendored file -> chain whose address space the entries belong to
_FILE_CHAINS: Mapping[str, str] = {
    "ofac_btc.json": "bitcoin",
    "ofac_eth.json": "ethereum",
    "ofac_usdt.json": "ethereum",
    "ofac_trx.json": "tron",
    "ofac_sol.json": "solana",
}


class OfacSanctionsSource:
    name = SOURCE_NAME

    def __init__(self, data_dir: Path | None = None) -> None:
        self._data_dir = data_dir or DATA_DIR

    def records(self) -> Iterable[LabelRecord]:
        seen: set[tuple[str, str]] = set()
        for filename, chain in _FILE_CHAINS.items():
            path = self._data_dir / filename
            if not path.exists():
                # A missing dataset silently disables sanctions screening for a
                # chain — a false negative in a forensic tool. Never silent
                # (REVIEW_FINDINGS.md, silent-degradation).
                logger.warning(
                    "OFAC dataset missing: %s — sanctions screening disabled for %s",
                    path,
                    chain,
                )
                continue
            addresses = json.loads(path.read_text())
            for address in addresses:
                record = LabelRecord(
                    chain=chain,
                    address=str(address),
                    entity="OFAC SDN listed address",
                    category=CATEGORY,
                    source=SOURCE_NAME,
                    confidence=CONFIDENCE,
                    source_date=OFAC_SNAPSHOT_DATE,
                )
                if record.key in seen:
                    continue  # e.g. an address on both the ETH and USDT lists
                seen.add(record.key)
                yield record
