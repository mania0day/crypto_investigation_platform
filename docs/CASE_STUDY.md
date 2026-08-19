# Case study — a sanctioned address that used a mixer and an exchange

**Subject:** `0xdcbeffbecce100cce9e4b153c4e15cb885643193` (Ethereum)

This is the case CipherChain ships with. It is a **real address carrying real funds**, not a
constructed demonstration, and every fact below was pulled from the chain and can be checked
independently on any block explorer.

---

## Why this case was chosen

It was not chosen from memory. It was chosen by **measurement**.

All 96 OFAC-sanctioned Ethereum addresses that CipherChain ships were probed against the mixer and
VASP label packs, asking a single question: *which of these actually transacted, directly,
with both a mixer and a named exchange?*

| Of 96 sanctioned addresses | Count |
|---|---|
| Touched a Tornado Cash pool directly | 2 |
| Touched a labelled VASP directly | 57 |
| **Touched both** | **1** — this one |

That distribution is itself worth reporting to an officer: at one hop, sanctioned funds reach
an **exchange** roughly 28× more often than they reach a **mixer**. The exchange is the
common case. The mixer is the exception — which is exactly why a tool that gives up at a
mixer fails on the cases that matter most, and why "follow it, but marked" was the right call.

> A first candidate — the Ronin Bridge / Lazarus attacker address, `0x098b716b…2f96` — was
> rejected on evidence. It is sanctioned, it has 1,430 transactions, and it does reach
> Binance directly. But it has **zero** direct Tornado Cash counterparties; that laundering
> ran through intermediary wallets first. A case study claiming direct contact would have
> been wrong, and an officer checking it on Etherscan would have found that in a minute.

---

## What the chain actually shows

1,252 transactions, fetched from Etherscan across both the native and token feeds.

### Money in — from exchanges

| Date | From | Amount |
|---|---|---|
| 2018-08-14 | OKX | 1.8430 ETH |
| 2018-09-02 | OKX | 4.3560 ETH |
| 2020-09-01 | Binance | 6,000 USDT |
| 2020-09-09 | Binance | 1.9950 ETH |
| 2020-10-17 | Binance | 10,000 USDT |
| 2021-04-27 | Binance | 40,000 USDT |
| 2022-04-21 | Binance | **119,610.36 USDT** |

Eleven inbound movements from OKX and Binance operational addresses.

### Money out — into a mixer

| Date | To | Amount |
|---|---|---|
| 2019-12-18 → 2020-06-03 | Tornado Cash — 0.1 ETH pool | 0.1 ETH × 11 deposits |
| 2020-05-13 | Tornado Cash — 100 DAI pool | 100 DAI |
| 2020-06-18 | Tornado Cash — 100 DAI pool | 100 DAI |

### The part that makes this case valuable

Two round trips through the **same pool, by the same address**:

| Deposit | Withdrawal |
|---|---|
| 2020-05-13 — 100.00 DAI out | 2020-05-13 — 92.63 DAI back in |
| 2020-06-18 — 100.00 DAI out | 2020-06-18 — 71.91 DAI back in |

That is **heuristic 1 — address match** — the strongest rung on the mixer exit ladder,
occurring in live data. The same address deposited and withdrew.

**But read what that actually buys, because it is less than it first appears.** The rung
fires and the match is recorded, yet the candidate it proposes is *the subject's own
address* — which is already the root of this trace. The engine drops it with the reason
`the candidate is the investigated address itself`, because re-admitting the subject one hop
deeper would restate a conclusion the run has already drawn, and restate it as a *guess*.

So on this case the strongest rung on the ladder **makes no forward progress**. That is the
engine behaving correctly — there is genuinely no new address on the far side to follow —
but it means the crossing here advances via the weaker rungs or not at all.

What the rung *does* deliver is investigative, not navigational: it is strong evidence that
**the subject controlled both sides of the pool crossing**. For an officer, "this address
deposited 100 DAI and withdrew 92.63 DAI from the same pool the same day" is a fact worth
having in its own right, independent of where the trail goes next.

---

## What this case exercises

| Capability | How it shows up here |
|---|---|
| Sanctions detection | Subject is on the OFAC SDN digital-currency list |
| **Backward objective** | Nearest previous VASP → Binance / OKX, on a citable third-party label |
| **Forward objective** | Nearest next hop → Tornado Cash, a mixer |
| **Mixer following** | The forward branch no longer stops — the ladder proposes exits |
| **Address-match heuristic** | Fires on the two real DAI round trips |
| **Speculative marking** | Post-mixer nodes are marked, drawn dashed, never reported as traced |
| **VASP metadata** | Binance and OKX both resolve to jurisdiction, legal entity, LE channel |
| **Asset-verification guard** | See below |

### A bonus demonstration nobody planned

On 2022-10-29 the subject received 1,000 units of a token whose symbol is literally
`$ TornadoV2.com - TornadoCash new link`, appearing to originate from the Tornado router
address.

This is a spam token with a deliberately deceptive name — a phishing lure, not a Tornado
Cash transfer. It is a live example of exactly the attack CipherChain's ranking guard exists to
stop: **a token contract can emit transfers naming any amount and any symbol it likes.** If
frontier ranking counted unverified assets, an attacker could spray a target with a
worthless token carrying an astronomical nominal amount and sink the real trail below the
spam until the budget ran out.

CipherChain ranks only on assets whose provenance is established. The spam is still recorded and
still explored — it simply does not get to decide what is explored *first*.

---

## What CipherChain is *not* claiming here

The vendored OFAC dataset
([0xB10C/ofac-sanctioned-digital-currency-addresses](https://github.com/0xB10C/ofac-sanctioned-digital-currency-addresses),
MIT, snapshot `2026-08-07`) carries **addresses only** — no designation names, no attributed
person or group. So CipherChain says *"this address is on the OFAC SDN digital-currency list as of
2026-08-07"* and stops there.

It does **not** name who controls it, because nothing in our evidence base supports that. An
investigator who needs the designation looks it up at treasury.gov; the tool will not guess
one for them. That restraint is the product working correctly, not a gap in it.

Likewise, every post-mixer address the exit ladder proposes is a **lead, not an
attribution**. Published linkage rates for Tornado Cash put all five heuristics stacked at
roughly one withdrawal in three, which means the common case is that the selection is wrong.
Each candidate therefore carries a mandatory plain-language weakness, and no mixer-derived
node can ever become a name — naming an operator requires a `third_party_claim`, and nothing
in the mixer package produces one.

---

## Running it

```bash
cd backend && ./scripts/demo.sh          # in one terminal

KEY='cc_…'                                # printed by demo.sh
curl -s -X POST http://127.0.0.1:8000/investigations \
  -H "Authorization: Bearer $KEY" -H 'content-type: application/json' \
  -d '{
        "address": "0xdcbeffbecce100cce9e4b153c4e15cb885643193",
        "chain": "ethereum",
        "objectives": ["find_prev_vasp", "find_next_vasp"],
        "budgets": {"api_calls": 400, "max_depth": 4, "max_nodes": 300}
      }'
```

`chain` is **required for this address**, and that is worth seeing. Omit it and the API
refuses rather than picking:

```json
{"detail": {"reason": "ambiguous",
            "message": "This address is active on more than one chain. Which one do you mean?",
            "candidates": ["ethereum", "polygon"]}}
```

The same 20-byte address exists on every EVM chain, and this one has real activity on two of
them. A tool that silently chose Ethereum would be right most of the time and silently wrong
the rest — with no way for the reader to tell which run they were holding.

Or open <http://127.0.0.1:8000/> and paste the address in — the UI runs both objectives and
draws the graph, with speculative branches dashed.

The full evidence-backed document is at `GET /investigations/{id}/report?format=pdf`.

---

## What CipherChain actually produced

Run live against Ethereum on 2026-08-16. Both figures below are from the real run, not a
rehearsal.

### Run 1 — budget 400 nodes

| | |
|---|---|
| Status | `partial` |
| Nodes reached | 400 (budget exhausted) |
| Transactions examined | 2,474 |
| API calls spent | **14** |
| Mixer stops / crossings | 1 / 0 |

**Backward — answered.** `nearest_named` = `0x28c6c062…21d60`, *Binance (operational
address)*, 1 hop, confidence 0.75.

**Forward — not answered.** The node budget ran out before the trace could cross a mixer.

> Note `api_calls: 14` against `nodes: 400`. Attribution resolves at discovery from the local
> label store, so 400 addresses were classified on 14 network calls. The binding constraint
> was **nodes, not quota** — which is why more API allowance would not have helped.

### Run 2 — resumed via `POST /investigations/{id}/resume`

| | |
|---|---|
| Status | `partial` (time budget) |
| Nodes reached | 1,573 |
| Transactions examined | 14,982 |
| API calls spent | 125 |
| Findings | 319 |
| Mixer stops / crossings | 4 / **1** |

**Both directions answered, both with a name:**

| Direction | Nearest named endpoint | Hop | Confidence | Speculative? |
|---|---|---|---|---|
| Backward | **Binance** (operational address) | 1 | 0.75 | No |
| Forward | **OKX** `0x2ce910fb…d8299` | 2 | 0.90 | **No** |

The `speculative: false` on both is the point: the named answers came by traced paths. The
mixer crossing happened, and it did **not** contaminate the headline.

### The mixer crossing, in the tool's own words

> *"the trail crossed this mixer on a heuristic (`mixer-exit-anonymity-set@1`): 2 candidate
> branch(es) followed as SPECULATIVE, 0 not followed — every address beyond this point may
> belong to an unrelated party"*
>
> — `heuristic_inference`: *"why these branches may be the wrong ones: one of 2 deposits in
> the anonymity set within 7 days; this is a lead, not an attribution"*

Four other mixer branches stopped instead, and said so without overclaiming:

> *"the trail stopped at this mixer — no exit candidate could be proposed; no branch past it
> is offered, **which is not evidence that none exists**"*

That final clause is the discipline in miniature. "We found nothing" and "there is nothing"
are different statements, and only one of them is true here.

### The phishing token showed up in the engine, exactly as predicted

One stopped branch names its pool as
`0xd90e2f92…:$ TornadoV2.com - TornadoCash new link#33258`. That is the spam token from
2022-10-29 being handled as a denomination — recorded, explored, and given no power to steer
the ranking. The guard worked on live data.

### The document

`GET /investigations/{id}/report?format=pdf` produced a **189-page PDF** (2.1 MB), saved to
`docs/case/`. Page 3 carries the filing metadata for Binance:

| Field | Value |
|---|---|
| Legal entity | Binance Holdings Limited |
| Jurisdiction | Cayman Islands |
| KYC in force since | 2021-08-20 |
| LE request channel | Binance Law Enforcement Request System (verified agency domains) |
| Reference source | US DOJ plea agreement, *United States v. Binance Holdings Limited* (D. Nev., 2023-11-21); CFTC complaint (N.D. Ill., 2023-03-27) |

That is the difference between an answer and a filable one.

### What it got wrong, or at least awkwardly

`nearest` in **both** directions is `0xc02aaa39…56cc2` — the **WETH contract** — classified
as *"service endpoint (operator unnamed): collects from 76 and pays out to 47 distinct
addresses — behaves as custodial infrastructure such as an exchange"* at 61%.

Behaviourally that is defensible; WETH does hold ETH on other people's behalf. But WETH is
not a VASP in any regulatory sense, and an officer skimming the top line could misread it.
The safety properties held — it is marked `OPERATOR NOT NAMED`, carries *"this address is a
lead, not a respondent"*, and `nearest_named` gives the real answer — but the
service-endpoint heuristic clearly does not distinguish a custodial *business* from a
custodial *contract*. That is a real limitation and it is listed as one.

Both runs ended `partial`, with 1,435 frontier addresses left unexplored and named samples
of them printed in the report. Neither run claimed to be complete.

---

## Reproducing the case selection

The sweep that chose this address is reproducible:

```bash
python scripts/probe_cases.py            # all 96, ~8 minutes
python scripts/probe_cases.py 0xdcbe…    # just this one
```

Raw output is kept at `docs/research/case-probe-results.json` so the choice can be audited
rather than taken on trust.
