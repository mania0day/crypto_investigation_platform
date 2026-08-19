"""The three exchanges asked for, and an honest account of reaching them.

The request was a daily refresh of hot-wallet coverage for **Coinbase,
Binance and OKX**, added to the ~75,000 labels already in the store. Then the
question that produced this file's current shape: *"is it updates daily"*.

:func:`daily_sources` is the cycle's whole source list and so returns more than
the three exchanges — the OFAC SDN list joins them from
:mod:`cipherchain.harvest.sanctions`. It stays here because the scheduler needs
one place to ask what runs today, and a second assembly point is how a source
gets written and then never scheduled.

Reachability, measured — not assumed
------------------------------------
From the host this was written on, on 2026-08-16, with a plain identifying
User Agent and no browser:

===============================  ===============================================
``www.coinbase.com``             ``robots.txt`` 200 and permits the path;
                                 ``/cbbtc/proof-of-reserves`` answers **200,
                                 349 KB**, server-rendered, carrying 52 Bitcoin
                                 addresses and its own ``lastUpdatedAt``.
                                 ``/legal/transparency`` — the url this module
                                 shipped with — **404s**
``www.binance.com``              ``robots.txt`` 200; the disclosure page answers
                                 **HTTP 202 with an empty body** — a bot-check
                                 interstitial, no content
``www.okx.com``                  the connection does not complete at all
===============================  ===============================================

So the answer to "does it update daily" is now: **one of the three does.**

- **Coinbase is automatic.** :func:`daily_sources` fetches the reserves page
  every cycle and parses it. No human involved.
- **Binance and OKX are manual-drop, and that is the correct answer for them.**
  202-with-no-body and a reset connection are deliberate bot blocking. Getting
  past a bot check means executing it, impersonating a browser, or rotating
  identity, and the fetch tier's boundaries
  (``providers/clients/explorer_fetch.py``) are the same boundaries here. This
  matches what ``scripts/import_por_labelpack.py`` recorded independently
  ("the endpoints sit behind bot protection and cannot be fetched from a
  script"). An operator downloads the published file on a machine that can
  reach it and drops it in the harvester's directory.

The drop path is not a degraded mode. The file is still the exchange's own
publication, it still declares its date, the claims still go through the intel
service, and the label lifecycle is untouched. What the operator supplies is
the transport, not the trust — which is why Coinbase keeps ONE identity across
both paths (:class:`FirstAvailableSource`) rather than becoming two sources
that could end up corroborating each other.

Doing the drop
--------------
Put the publisher's own file in the drop directory (``--drop-dir``,
``$CIPHERCHAIN_DROP_DIR``, or ``./drops``), named for the source and dated with the
**publication date, not today's**::

    binance-proof-of-reserves__2026-08-14.json   a labelpack (labels/README.md)
    binance-proof-of-reserves__2026-08-14.csv    a published proof-of-reserves file
    okx-proof-of-reserves__2026-08-14.csv
    coinbase-cbbtc-reserves__2026-08-14.html     a saved copy of the reserves page,
                                                 for a host with no route to it

Older drops are left in place; the newest declared date wins. The file name's
date is deliberately not the file's mtime: mtime records when the file was
COPIED, and what a reader needs in order to weigh a claim is when the
disclosure was PUBLISHED. That date is also what the staleness alarm reads, so
an operator who stops dropping files does not get a green cycle for it — see
:mod:`cipherchain.harvest.scheduler`.

Sources that were checked and are NOT here
------------------------------------------
This section used to say that the OFAC SDN download, the UK OFSI consolidated
list and the EU financial sanctions file "all time out from this host". **That
was wrong about all three**, and the correction is worth more than the note
was: it had been used as the reason CipherChain read sanctions from a
hand-refreshed snapshot instead of a feed. Re-measured 2026-08-18, same host,
same plain User-Agent:

- ``sanctionslistservice.ofac.treas.gov/api/download/sdn.xml`` **works**. It
  answers 302 onto OFAC's own signed GovCloud url and serves 28,812,494 bytes
  at ~77 KB/s. The original probe used a 200-second ceiling on a six-minute
  download, so what it measured was the ceiling. The SDN list is now a fetched
  daily source — see :mod:`cipherchain.harvest.sanctions`, which carries the
  numbers, the three urls that genuinely do not work, and the reason a partial
  download of it must be refused rather than ingested.
- ``ofsistorage.blob.core.windows.net`` (UK OFSI) and
  ``webgate.ec.europa.eu`` (EU) also **answer 200**, with 54,098,673 and
  25,766,640 bytes respectively. Neither is implemented: each is a different
  schema needing its own parser and currency-to-chain mapping. They are absent
  for want of that work, not for want of a route — which is a different
  backlog item than the one this file used to imply.

Kraken, Bybit, KuCoin and crypto.com serve their proof-of-reserves pages (200)
but publish Merkle attestations rather than address lists, so there is nothing
in them to label.
Aggregated community lists (``ethereum-lists``, DefiLlama's CEX addresses) ARE
reachable and are still not here: they are neither first-party nor licensed,
:data:`TRUSTED_METHODS <cipherchain.intel.policy.TRUSTED_METHODS>` would refuse
them at construction, and calling one a ``licensed_dataset`` to get it past
that check would put crowd-maintained attributions straight into active,
citable labels.

What these addresses ARE
------------------------
A proof-of-reserves address is a wallet holding reserves. It answers "these
funds reached Binance" well and "this is the wallet customer deposits are
swept into" less well; deposit-address coverage comes from clustering
(REACHING_THE_VASP.md §6.1), not from here.
"""

from __future__ import annotations

from pathlib import Path

import httpx

from cipherchain.harvest.parsers import PARSERS_BY_SUFFIX, parse_coinbase_reserves
from cipherchain.harvest.sanctions import sanctions_sources
from cipherchain.harvest.sources import (
    DocumentParser,
    FirstAvailableSource,
    HarvestSource,
    HttpDocumentSource,
    ManualDropSource,
    SourceSpec,
)

# Confidence sits below the signature tier on purpose: nothing in this module
# checks a key, so these rest on the exchange's publication and the operator's
# handling of the file, which is weaker than a signature anyone can re-verify.
_PUBLISHED = "first_party_published"

# The reserves page restates itself continuously — the `lastUpdatedAt` measured
# on 2026-08-16 was that same afternoon. Three days is therefore already a
# loud signal for it, where it would be meaningless noise for a monthly
# publisher.
COINBASE = SourceSpec(
    name="coinbase-cbbtc-reserves",
    entity="Coinbase",
    method=_PUBLISHED,
    document_url="https://www.coinbase.com/cbbtc/proof-of-reserves",
    stale_after_days=3,
)

BINANCE = SourceSpec(
    name="binance-proof-of-reserves",
    entity="Binance",
    method=_PUBLISHED,
    document_url="https://www.binance.com/en/proof-of-reserves",
)

OKX = SourceSpec(
    name="okx-proof-of-reserves",
    entity="OKX",
    method=_PUBLISHED,
    document_url="https://www.okx.com/proof-of-reserves",
)

EXCHANGE_SPECS: tuple[SourceSpec, ...] = (COINBASE, BINANCE, OKX)

# Coinbase alone reads HTML, because Coinbase alone publishes a page this
# package can read. Keeping it out of PARSERS_BY_SUFFIX is the point: a `.html`
# drop for any OTHER source has no parser and is refused, rather than being
# handed to a reader written for one specific page.
COINBASE_PARSERS: dict[str, DocumentParser] = {
    **PARSERS_BY_SUFFIX,
    "html": parse_coinbase_reserves,
}


def manual_drop_sources(drop_dir: Path) -> list[HarvestSource]:
    """The three exchange sources, all reading from one drop directory.

    The offline shape of the cycle: no network at all, every source fed by
    hand. :func:`daily_sources` is what the scheduler runs.

    Returned even when the directory is empty or missing: a source with nothing
    to read reports ``unavailable`` for the cycle, which is a fact worth having
    in the report. Silently returning a shorter list would make "no drop this
    week" indistinguishable from "this source was never configured".

    The three EXCHANGES, and not every source with a drop path — the SDN list
    has one too (:func:`cipherchain.harvest.sanctions.ofac_sdn_source`). Anyone
    assembling a fully offline cycle needs both, and getting only this one back
    would leave sanctions coverage quietly absent rather than reported missing.
    """
    return [
        ManualDropSource(
            spec,
            drop_dir,
            parsers=COINBASE_PARSERS if spec.name == COINBASE.name else PARSERS_BY_SUFFIX,
        )
        for spec in EXCHANGE_SPECS
    ]


def daily_sources(drop_dir: Path, http: httpx.AsyncClient) -> list[HarvestSource]:
    """What the daily cycle actually runs: fetch what is fetchable, drop the rest.

    Coinbase gets both transports under one source identity — the fetch first,
    the drop behind it. That ordering is the whole point: on a host with a
    route to coinbase.com nobody has to do anything, and on a host without one
    the operator's saved page still lands as the same source's claims.

    Binance and OKX get the drop only. Pointing an :class:`HttpDocumentSource`
    at either would add a request that is known to come back empty, every day,
    to a site that has already said no.

    OFAC's SDN list is fetched, and is the one source here where a missed cycle
    is a false negative rather than thinner coverage: an address designated
    yesterday and absent from the store makes a trace through it report a clean
    chain. It is last in the list because it is by far the largest download
    (~28 MB, minutes) and the three exchange sources should have committed
    before it starts — each source commits in its own session, so a cycle
    interrupted during the SDN fetch still keeps everything ahead of it.
    """
    coinbase = FirstAvailableSource(
        COINBASE,
        [
            HttpDocumentSource(COINBASE, http, parser=parse_coinbase_reserves, media="html"),
            ManualDropSource(COINBASE, drop_dir, parsers=COINBASE_PARSERS),
        ],
        parsers=COINBASE_PARSERS,
    )
    return [
        coinbase,
        *(ManualDropSource(spec, drop_dir, parsers=PARSERS_BY_SUFFIX) for spec in (BINANCE, OKX)),
        *sanctions_sources(drop_dir, http),
    ]
