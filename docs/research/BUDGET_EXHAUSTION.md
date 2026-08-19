# Why a real theft trace runs out of budget

**Status:** ✅ FIXED — "rank by verified assets only" shipped and confirmed live (see §5) · measured 2026-08-10 on a live trace
rooted at a Bybit Exploiter address (`0x0fa09c3a…`), `api_calls=25, max_depth=4, max_nodes=400`

---

## 1. Only one budget actually binds

| budget | spent | cap | |
| --- | --- | --- | --- |
| `api_calls` | 25 | 25 | **100% — binding** |
| `max_nodes` | 157 | 400 | 39% |
| `seconds` | 47 | 600 | 8% |

Raising `max_nodes` or `seconds` would change nothing. And `api_calls` is charged **once per address
expansion**, while an EVM expansion makes **three** provider calls (`txlist`, `tokentx`,
`txlistinternal`) — so 25 "api_calls" was really ~75 upstream requests. The budget is a traversal
bound wearing a quota name.

## 2. The shape of the search

26 nodes processed produced 157 nodes — **≈6 new nodes per expansion**. The trace finished hops 0–2
and never began hop 3:

| hop | expanded | terminal | frontier |
| --- | --- | --- | --- |
| 0 | 1 | | |
| 1 | 9 | 1 | |
| 2 | 14 | 1 | 6 |
| 3 | | | **125** |

Clearing hop 3 would need ~131 more expansions and create roughly 790 further nodes — past
`max_nodes=400`. **On a graph this wide, completion is not reachable at any sane budget.** That is a
property of the case, not a defect, and the coverage statement already says so.

Also: 14 of the 26 expanded addresses had **truncated history**, so even the explored ones were read
only in part.

## 3. The finding that matters: spam tokens set the traversal priority

Claim order is hop ascending, then `value_share` descending. The top five unexplored branches:

| address | value_share | backed by |
| --- | --- | --- |
| `0x98f1e572…` | 2.07 × 10²⁶ | XEN (unverified token) |
| `0xd3fd2e73…` | 1.38 × 10²⁶ | XEN |
| `0xe69a8119…` | 6.94 × 10²⁵ | XEN |
| `0x327a9e68…` | 6.88 × 10²⁵ | XEN |
| `0xeb5add95…` | 8.68 × 10²⁴ | GOOK |

The largest **native** movement in the entire trace is `3.22 × 10²⁰` wei — **322 ETH, six orders of
magnitude smaller.** So the priority queue is ordered almost entirely by unverified token amounts,
and the genuine ETH flows rank *below* the spam.

Two compounding causes:

1. **`value_share` is dimensionally incoherent.** Amounts are compared in each asset's own smallest
   unit, so a token with 18 decimals outranks one with 6 by 10¹² before any real value is
   considered — and both are compared against wei.
2. **The asset provenance floor governs evidence, not traversal.** That was a deliberate choice, and
   it is right for *evidence*: forged-asset movements are real events and hiding them from the graph
   would be its own dishonesty. But it means unverified tokens still create frontier nodes **and set
   their rank**. 47% of the movements stored on this trace (897 of 1,922) are in unverified assets.

The spray is visible in the data: six different fake tokens — including a counterfeit `USDC` at
`0x573d93…` and an `mUSDT` — each appear exactly once, each with the *identical* amount
`1.394 × 10⁴²`. That is address poisoning, not organic activity.

**Consequence worth naming plainly:** an attacker can steer CipherChain's traversal by spraying a victim
with a worthless token carrying an astronomical nominal amount. It costs gas and it pushes real
branches down the queue until the budget runs out. This is the same asset-forgery family the
provenance floor was built for, arriving at a surface that floor deliberately does not cover.

## 4. Options, not yet chosen

Recorded for a ruling, in rough order of confidence:

- **Rank by verified assets only.** Compute `value_share` from evidence-grade movements, leaving
  unverified-asset counterparties in the frontier but at the bottom. Keeps traversal complete,
  removes attacker control of ordering. Cheapest and most targeted.
- **Make value comparable.** Normalise by `decimals` before ranking. Fixes the dimensional error but
  not attacker control — a fake token can still name any number.
- **Charge the real call count.** Make `api_calls` mean provider calls, so budgets mean what they
  say. Independent of the above and arguably owed regardless.
- **Report the ordering basis.** Whatever ranks the frontier should be visible in the coverage
  statement, so an investigator can see the trace was steered.

Nothing here changes what a finding *claims*, so this is a traversal-policy decision rather than a
semantic one — but it does change which answers get found first, so it wants an explicit ruling.


---

## 5. Fix shipped and confirmed (2026-08-10)

**Ranking now counts only movements in assets whose provenance is established.** Unverified-asset
counterparties are still discovered, still stored, still explored and still appear in evidence — they
simply rank at zero instead of dominating the queue. The provenance floor continues to govern
evidence only; forged transfers are real on-chain events and hiding them would be its own dishonesty,
and would also erase an attacker's footprints.

Confirmed by re-running the same Bybit trace at the same budgets:

| | before | after |
| --- | --- | --- |
| top-ranked branch | `2.07 × 10²⁶` (XEN, unverified) | `5.87 × 10²⁰` (WETH, verified) |
| the genuine 322 ETH movement | outranked by six orders of magnitude | **ranked #2** |
| nodes ranked zero | 0 | **37 of 119** (the unverified ones) |
| forged movements in the graph | present | **still present** — XEN still carries 98 movements at max `8.72 × 10²⁹` |
| hop-3 frontier | 125 | 88 |
| findings | 35 | 36, including a **`bridge_crossing` the earlier run never reached** |

That last row is the point: with the spam demoted, the budget reached a real branch it had previously
been starved out of.

Permanent regression fixture in `tests/analysis/test_asset_forgery.py` reproduces the exact
poisoning shape — six counterfeit contracts, each seen once, each naming the identical absurd amount
— and asserts both halves: the genuine branch ranks first, and the forged counterparties still enter
the graph and remain reachable.

The coverage statement now says so to the reader: *"exploration order ranks by value in verified
assets only, so a large movement in an unverified token is explored late rather than first."*

**Observation for later, not a defect:** with spam demoted, the top-ranked branch became the **WETH
contract itself** — a verified asset, but a token contract rather than a wallet, and not
forensically interesting to expand. The supernode guard catches it, so budget loss is bounded. Worth
revisiting alongside contract detection.

**Still open, deliberately deferred:** decimal normalisation (ranking still compares smallest units
across assets, so an 18-decimal asset outranks a 6-decimal one by 10¹² before real value enters), and
charging `api_calls` per provider call rather than per expansion.
