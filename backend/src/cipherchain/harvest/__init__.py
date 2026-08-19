"""The harvester: keep the label store fresh without going around its rules.

Coverage is what decides whether a trace can name a VASP — a run that comes
back unnamed is usually a missing label, not clean money (REACHING_THE_VASP.md
§2, case 4). So this package refreshes the sources CipherChain is allowed to trust
and hands every row to ``IntelService``, which owns the lifecycle. Nothing here
writes to the label store, and nothing here decides what may name an operator.

    sources.py     what a source is: a document, its provenance, one parser
    parsers.py     one pure parser per published format
    exchanges.py   Coinbase / Binance / OKX, and how the files actually arrive
    sanctions.py   OFAC's SDN list — fetched whole, or not ingested at all
    worker.py      the cycle — refresh every source, then reconcile once
    scheduler.py   the daily cron entry point

Does it update daily? Partly, and the split is the honest bit
--------------------------------------------------------------
================  =========  ===================================================
source            transport  what an operator has to do
================  =========  ===================================================
Coinbase          automatic  nothing. ``coinbase.com/cbbtc/proof-of-reserves``
                             is server-rendered, robots permits it, and the
                             cycle fetches and parses it every day
OFAC SDN          automatic  nothing. The published download works from here
                             (measured 2026-08-18, contradicting an earlier note
                             in ``exchanges.py``) and the cycle fetches all
                             ~28 MB of it daily
Binance           **manual** download the published proof-of-reserves file on a
                             machine that can reach binance.com and drop it in
                             ``--drop-dir``. From here the page answers HTTP 202
                             with an empty body — a bot check
OKX               **manual** same, and from here okx.com does not connect at all
================  =========  ===================================================

Working around either bot check is out of bounds (``explorer_fetch.py`` holds
the same line), so manual-drop is the right answer for those two rather than a
thing left to fix. Name the drop for its source and its PUBLICATION date::

    <drop-dir>/binance-proof-of-reserves__2026-08-14.csv
    <drop-dir>/okx-proof-of-reserves__2026-08-14.csv
    <drop-dir>/coinbase-cbbtc-reserves__2026-08-14.html   (only if this host
                                                           cannot reach it)
    <drop-dir>/ofac-sdn__2026-08-18.xml                   (likewise; date it
                                                           the list's own
                                                           Publish_Date)

Then run ``scripts/harvest.sh`` — or let cron do it. If a source stops being
republished, the cycle says so by name and exits 3; it does not go quiet.
:mod:`cipherchain.harvest.exchanges` carries the measurements behind all of this,
and :mod:`cipherchain.harvest.scheduler` the exit codes.
"""

from cipherchain.harvest.exchanges import (
    BINANCE,
    COINBASE,
    COINBASE_PARSERS,
    EXCHANGE_SPECS,
    OKX,
    daily_sources,
    manual_drop_sources,
)
from cipherchain.harvest.parsers import (
    PARSERS_BY_SUFFIX,
    SDN_LEDGER_CHAINS,
    parse_coinbase_reserves,
    parse_labelpack,
    parse_ofac_sdn,
    parse_proof_of_reserves_csv,
)
from cipherchain.harvest.sanctions import (
    OFAC_PARSERS,
    OFAC_SDN,
    OFAC_SDN_TIMEOUT_SECONDS,
    OFAC_STORAGE_HOSTS,
    ofac_sdn_source,
    sanctions_sources,
)
from cipherchain.harvest.sources import (
    DEFAULT_STALE_AFTER_DAYS,
    DocumentLoader,
    DocumentParser,
    FirstAvailableSource,
    HarvestDocument,
    HarvestError,
    HarvestSource,
    HttpDocumentSource,
    ManualDropSource,
    SourceRejected,
    SourceSpec,
    SourceUnavailable,
)
from cipherchain.harvest.worker import HarvestReport, HarvestWorker, SourceOutcome

__all__ = [
    "BINANCE",
    "COINBASE",
    "COINBASE_PARSERS",
    "DEFAULT_STALE_AFTER_DAYS",
    "EXCHANGE_SPECS",
    "OFAC_PARSERS",
    "OFAC_SDN",
    "OFAC_SDN_TIMEOUT_SECONDS",
    "OFAC_STORAGE_HOSTS",
    "OKX",
    "PARSERS_BY_SUFFIX",
    "SDN_LEDGER_CHAINS",
    "DocumentLoader",
    "DocumentParser",
    "FirstAvailableSource",
    "HarvestDocument",
    "HarvestError",
    "HarvestReport",
    "HarvestSource",
    "HarvestWorker",
    "HttpDocumentSource",
    "ManualDropSource",
    "SourceOutcome",
    "SourceRejected",
    "SourceSpec",
    "SourceUnavailable",
    "daily_sources",
    "manual_drop_sources",
    "ofac_sdn_source",
    "parse_coinbase_reserves",
    "parse_labelpack",
    "parse_ofac_sdn",
    "parse_proof_of_reserves_csv",
    "sanctions_sources",
]
