"""Sanctions listings, and the first one that keeps itself current.

OFAC's SDN list is what lets a trace say "this reached a sanctioned address"
at all. Until this module it reached CipherChain as a vendored snapshot
(:mod:`cipherchain.analysis.sanctions.ofac`, dated ``2026-08-07``) that a
person had to refresh by hand — so an address designated on Tuesday did not
exist as a sanctioned address here until somebody remembered to re-run a
script. That is the same quiet-decay failure the rest of this package was
built around, in the one place where the consequence is worst: a trace through
an address OFAC designated last week reports "no named endpoint", which reads
as *the money went nowhere interesting* rather than *the store is behind*.

Reachability, measured — not assumed
------------------------------------
From this host on 2026-08-18, with a plain identifying User-Agent and no
browser:

All three requests were made against ``sanctionslistservice.ofac.treas.gov``:

=========================  ===================================================
``/robots.txt``            **404** — the site publishes no rules, which is the
                           documented allow-all and is how ``RobotsPolicy``
                           reads it
``/api/download/sdn.xml``  **302** onto the GovCloud bucket named in
                           :data:`OFAC_STORAGE_HOSTS`, carrying
                           ``X-Amz-Expires=3600`` — a signed url good for one
                           hour, not a relocation
the redirect's target      **28,812,494 bytes**, ``Publish_Date`` ``08/18/2026``,
                           ``Record_Count`` ``19202``, 977 digital-currency id
                           rows, closing ``</sdnList>``
=========================  ===================================================

A full cycle through this source, end to end, was then run against the live
endpoint: 28,812,494 bytes in 196 s, parsed in under a second, **914 sanctioned
addresses** — 530 bitcoin, 276 tron, 104 ethereum, 4 solana.

``robots.txt`` is read on the endpoint's host and not again on the bucket. The
bucket url is not a page anybody could crawl: it exists because we asked this
publisher for this document a second ago, it is signed to us, and it stops
working within the hour. There is no crawling decision left for a second
``robots.txt`` to make.

Three urls that do NOT work, recorded so they are not tried again:
``www.treasury.gov/ofac/downloads/sdn.xml`` (no route),
``sanctionslistservice.ofac.treas.gov/api/PublicationPreview/exports/SDN.XML``
(no route), and ``ofac.treasury.gov/media/13581/download?inline`` (a 404 page,
which is worse than a dead host — it answers 200-shaped nonsense to a careless
reader).

The note this module replaces said the OFAC downloads "time out from this
host". They do not. That measurement was taken with a 200-second ceiling
against a document that needs about six minutes at the throughput this host
gets, so what it recorded was the ceiling, not the endpoint — and the source
was written off on it. :data:`OFAC_SDN_TIMEOUT_SECONDS` carries the arithmetic
so the same mistake cannot be repeated by tidying.

Why the size is a correctness problem, not a performance one
------------------------------------------------------------
That 200-second probe did not come back empty. It came back with 15,388,672
bytes — 53% of the document — holding 9,959 complete ``<sdnEntry>`` elements
(the 9,960th was still open where the cut landed) and 564 of the document's 977
digital-currency rows. A truncated SDN document parses far enough to
look like a working day's harvest, and if it is ingested the addresses past the
cut simply stop being sanctioned: no error, no gap in the report, just a store
that is quietly missing half a sanctions list. So this source fails CLOSED —
:func:`cipherchain.harvest.parsers.parse_ofac_sdn` proves the document reached
its closing element before it builds a single claim, and an incomplete body is
:class:`SourceUnavailable` (keep yesterday's rows, try again tomorrow) rather
than a small harvest.

What is NOT here
----------------
The UK OFSI consolidated list and the EU financial sanctions file. Both were
measured the same day and both ANSWER — ``ofsistorage.blob.core.windows.net``
returns 200 with 54,098,673 bytes, ``webgate.ec.europa.eu`` 200 with
25,766,640 bytes — which contradicts the older note in
:mod:`cipherchain.harvest.exchanges` that said all three sanctions sources time
out. They are absent because each is a different schema needing its own parser
and its own currency-to-chain mapping, not because they are unreachable. That
is a day of work with a known starting point, recorded here so the next person
does not start by re-deriving reachability.
"""

from __future__ import annotations

from pathlib import Path

import httpx

from cipherchain.harvest.parsers import parse_ofac_sdn
from cipherchain.harvest.sources import (
    DocumentParser,
    FirstAvailableSource,
    HarvestSource,
    HttpDocumentSource,
    ManualDropSource,
    SourceSpec,
)
from cipherchain.investigation.attribution import CATEGORY_SANCTIONED, AddressRole

# OFAC publishes its own list, so this is the primary text rather than a
# redistribution of it — the same tier the exchanges' own disclosures get, and
# the reason these claims may name the party they designate on arrival.
_PUBLISHED = "first_party_published"

# The published download endpoint. It is the ONLY one of the four urls tried
# that serves the document (see the module docstring); the other three are
# recorded there so a future reader does not re-discover them the slow way.
OFAC_SDN_URL = "https://sanctionslistservice.ofac.treas.gov/api/download/sdn.xml"

# The endpoint hands the download off to OFAC's own GovCloud bucket with a
# one-hour signed url. Named here rather than allowed generally because
# `HttpDocumentSource` otherwise stops dead on a 3xx: a document that has
# genuinely MOVED needs a person to read what is at the new address before
# anything is harvested off it. This host is the one exception, reviewed once,
# at authoring time, and written down.
OFAC_STORAGE_HOSTS = frozenset({"wc2h-sls-prod-public-published.s3.us-gov-west-1.amazonaws.com"})

# Fifteen minutes, and here is the arithmetic, because the probe that wrote
# this source off allowed 200 s and concluded the endpoint was unreachable —
# which is how CipherChain ended up with a hand-refreshed sanctions snapshot
# instead of a daily source.
#
#   measured 2026-08-18 from this host: 28,812,494 bytes at ~77 KB/s
#   28,812,494 / (77 * 1024) ~= 366 s just to stream it, on a good run
#   28,812,494 / 900         ~= 31 KB/s, the slowest run that still completes
#
# So the ceiling holds down to about two-fifths of the slowest throughput
# measured. It has to: a full fetch through this source later the same day
# completed in 196 s (144 KB/s), so the rate this host gets varies by about 2x
# within a day, and a ceiling sized to a good run is a source that fails on the
# bad ones. httpx applies this per socket operation rather than to the transfer
# as a whole, so what it directly bounds is how long the stream may go quiet —
# but it is sized against the whole transfer anyway, because that is the number
# a reader will check it against, and because the shared client's 30 s
# (``scheduler.FETCH_TIMEOUT_SECONDS``, correct for a 350 KB page) is the value
# this must not silently drift back to. At 30 s the source does not fail
# loudly: robots and the redirect both still succeed, and the failure lands as
# one more "unavailable" line in a report that has them every day.
OFAC_SDN_TIMEOUT_SECONDS = 900.0

OFAC_SDN = SourceSpec(
    name="ofac-sdn",
    # The PUBLISHER, not the subject — and unlike every other source here, this
    # value never reaches a label. A sanctions listing is about the party named
    # in it, so `parse_ofac_sdn` puts the designated party on each claim and
    # this string exists only to say whose document it is.
    entity="OFAC SDN",
    method=_PUBLISHED,
    document_url=OFAC_SDN_URL,
    # Not the VASP default. `corroborates()` requires a matching category, so a
    # sanctions listing filed as `vasp` could confirm a claim that an address
    # is an exchange — which is a different assertion about the same address,
    # and the store would show them agreeing.
    category=CATEGORY_SANCTIONED,
    # OFAC says whose address it is, never what it is FOR. `operational` (the
    # spec default) would assert the designated party's own wallet, and
    # designations include deposit addresses at exchanges.
    role=str(AddressRole.UNKNOWN),
    # The highest confidence any source in this package carries, because this
    # is the designating authority publishing its own list. Still not 1.0: an
    # address can be delisted between publications, and nothing in CipherChain
    # presents a claim as proof.
    confidence=0.95,
    # OFAC republishes on every designation action — the document fetched on
    # 2026-08-18 carried `Publish_Date` 08/18/2026, that same day — which in
    # practice is several times a month. 14 days is deliberately looser than
    # that: a fortnight with no action happens over holidays, and an alarm that
    # fires on a normal quiet fortnight is an alarm somebody mutes, which
    # leaves the subsystem with no alarm at all. A month of silence from OFAC
    # would be genuinely unusual and is what this is meant to catch.
    stale_after_days=14,
)

# `xml` and nothing else. The exchange sources accept a labelpack or a
# proof-of-reserves CSV as a drop; here that would let a stray file land as
# sanctions claims — the most consequential category in the store — under
# OFAC's name. A drop that is not the SDN document has no parser and is
# refused.
OFAC_PARSERS: dict[str, DocumentParser] = {"xml": parse_ofac_sdn}


def ofac_sdn_source(drop_dir: Path, http: httpx.AsyncClient) -> HarvestSource:
    """The SDN list: fetched every cycle, with the operator's drop behind it.

    One source identity across both transports, for the reason
    :class:`FirstAvailableSource` exists — the store keys claims on
    ``(chain, address, source)`` and ``corroborates()`` only asks for a
    DIFFERENT source, so a fetched copy and a hand-saved copy of the same
    document as two sources would corroborate each other.

    The drop is a genuine fallback here rather than the primary path it is for
    Binance and OKX: this endpoint is reachable and wants no human. It exists
    because a host that loses its route to treasury.gov should slow the
    sanctions list down to a person's pace, not remove it — save the document
    as ``ofac-sdn__<YYYY-MM-DD>.xml``, dated the list's own ``Publish_Date``.
    """
    return FirstAvailableSource(
        OFAC_SDN,
        [
            HttpDocumentSource(
                OFAC_SDN,
                http,
                parser=parse_ofac_sdn,
                media="xml",
                timeout=OFAC_SDN_TIMEOUT_SECONDS,
                follow_redirect_hosts=OFAC_STORAGE_HOSTS,
            ),
            ManualDropSource(OFAC_SDN, drop_dir, parsers=OFAC_PARSERS),
        ],
        parsers=OFAC_PARSERS,
    )


def sanctions_sources(drop_dir: Path, http: httpx.AsyncClient) -> list[HarvestSource]:
    """Every sanctions source in the daily cycle. One, so far — see the module
    docstring for what OFSI and the EU would each still need."""
    return [ofac_sdn_source(drop_dir, http)]
