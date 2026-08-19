# Harvest fixtures

Every harvest source is testable with **zero network access**. For two of the
three that is not a preference but the only option: from the host this was
written on, `binance.com` answers a bot-check interstitial and `okx.com` does
not connect at all (measured 2026-08-16, third verification). For Coinbase it
is a choice — that page IS fetchable from here — because a test that needs the
live site is a test that fails the day the site is slow, and a recording is
also the only way to pin what the parser does when the page changes shape.

| file | what it is |
|---|---|
| `binance_labelpack.json` | the repo's own labelpack format (`labels/README.md`), exactly as an operator would drop one |
| `okx_proof_of_reserves.csv` | a proof-of-reserves address file **written to the layout `scripts/import_por_labelpack.py` documents from a live OKX download** — the per-coin summary table first, then the real header row naming an address and the message signed over it |
| `coinbase_cbbtc_reserves.html` | a **real recording**, pruned: `https://www.coinbase.com/cbbtc/proof-of-reserves` fetched 2026-08-16 |
| `ofac_sdn.xml` | a **real recording**, cut down: OFAC's SDN list fetched 2026-08-18, 8 of its 19,202 entries kept verbatim |

The CSV is stated plainly as synthesised to a documented layout rather than
downloaded, because it was not downloaded. It carries the cases the parser has
to get right: the summary preamble that is not a header, four supported
networks, one network CipherChain has no adapter for, and blank rows.

The Coinbase page is the opposite: downloaded, then cut down. The live page is
349 KB and held 52 Bitcoin addresses; what is kept is the `server-app-state`
JSON island, the same double-encoded `relayStoreData`, the same Relay
`recordMap` shape, the first three `ReserveAddress` records with the
`ReserveBalance` records they point at, and the `ProofOfReserves` record with
its real `lastUpdatedAt` of `2026-08-16T16:32:11Z`. Two things were added
rather than recorded, and both are marked here so nobody mistakes them for
observations:

- one `ReserveAddress` on Cardano, so the "chain CipherChain cannot trace" branch is
  exercised. Coinbase publishes the same page for cbADA, cbXRP, cbDOGE and
  cbLTC, so this row is the real shape of a real case — just not one that
  appeared on the cbBTC page.
- a second `<script type="application/json">` block, because the parser keys on
  the element *id* and something has to prove that it does.

## `ofac_sdn.xml`

Downloaded, then cut — nothing in it is invented. The live document is
28,812,494 bytes with 19,202 `<sdnEntry>` elements and 977 digital-currency id
rows; eight entries are kept **byte for byte**, including OFAC's own
`publshInformation` misspelling, its real `Publish_Date` of `08/18/2026`, and
the namespace the service actually serves (which is its own hostname, not the
`tempuri.org` in the published schema). One value was edited: `Record_Count`,
lowered from 19,202 to the 8 entries that remain, because leaving it would be
the one line in the file asserting something untrue.

The eight were chosen to cover every branch of `parse_ofac_sdn` with real
published rows rather than constructed ones:

| entry | what it proves |
|---|---|
| OKO DESIGN BUREAU | the ordinary case — XBT, ETH and TRX on one designated entity |
| VALERIAN LABS, INC. | the same 0x address published under USDT, USDC **and** ETH: three rows, one claim |
| GARANTEX EUROPE OU | `Digital Currency Address - USDT` on a **Bitcoin** address (Omni-layer USDT) — the ticker cannot name the ledger, the encoding can |
| Ali SHAFIU | USDT on Tron, and an individual (`firstName` + `lastName`) rather than an entity |
| SHPS SHELBIT | Solana, long bech32 Bitcoin addresses, and an `akaList` holding `SHELBIT EXCHANGE` — the entry must not be named after its own alias |
| Dmitrii KARASAVIDI | XMR, LTC, ZEC, DASH, BTG — five unmodelled ledgers to skip — plus the same 0x address under both `ETH` and `ETC` |
| CRYPTO HOME DMCC | a `BNB` row, one of the tickers OFAC added recently |
| GALAXY MANAGEMENT NV | no crypto at all, and an `Executive Order 13846 information:` id row that must not be read as a currency |

Truncated documents are **not** kept as fixtures. The tests cut this file at a
byte offset instead, so what they refuse is provably a prefix of a document
that otherwise parses — which is exactly what a cut-off download produces, and
what a hand-written broken file could only imitate.
