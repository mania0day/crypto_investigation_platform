# Deposit-Address Discovery — Design

**Status:** ⛔ SUPERSEDED — this design was **not built**. See [`DEPOSIT_ADDRESS_DECISION.md`](DEPOSIT_ADDRESS_DECISION.md) for why (circular value case, non-separating confidence bands, yield inversely correlated with coverage need) and for the five shipped-code defects this review found instead. Retained because §2's findings on asset forgery and control inversion remain valid and shaped the shipped asset floor. · originally: DESIGN, AWAITING RULINGS · no code written · **Source:** 10-agent pass (4 verifiers,
4 designers, 2 adversarial critics) against HEAD `ac4266f`, 2026-08-09

> **Both critics returned `needs_revision`, and the forensic adversary found the heuristic does not
> survive contact in its originally-conceived form.** Two findings change what this feature is allowed
> to claim, and one of them is exploitable by a third party for a few dollars of gas. Read §2 before §3.

---

## 1. What is stale in `DEPOSIT_ADDRESS_GAP.md`

You asked me to re-verify my own doc rather than trust it. Most of it holds; the load-bearing
recommendation does not.

| Claim | Verdict |
| --- | --- |
| Sweeps detected, persisted, consumed by nothing | ✅ confirmed |
| Exactly one attributor call site, address-only, never re-consulted | ✅ confirmed (line moved to `engine.py:232`) |
| Detector contract is structurally blind | ✅ confirmed |
| No attributions table; `NodeRow` has no entity column | ✅ confirmed |
| **"First place it would need to be consumed: `_run_detectors`"** | ❌ **WRONG** |
| "`_assess_service` is the precedent" | ✅ confirmed, and now *more* apt |
| `EdgeRow` connects the swept address and the hot wallet | ⚠️ partly — edges are discovery-oriented, not value-oriented |

**The error is mine and it was introduced by last milestone.** `_run_detectors` now early-returns on
`has_processed_sibling` (`engine.py:425-428`) — the guard I added to stop duplicate behavioural
findings. A direction-stamped assessment placed there fires for at most one direction per address, and
for an address whose sibling terminated at the mixer/VASP/depth branches it fires **zero** times. A
deposit-address heuristic also cannot be a `Detector` at all: the signature (`engine.py:53-55`) hands it
no attributor and no repository, so it can neither resolve `to_address_id → Address` nor attribute it.

One further correction: `find_sweep_matches` returns a **flat list with no destination grouping**. A
deposit address sweeps into *one* collector; an address sweeping into twelve is a relay. That grouping
does not exist today and must be built.

---

## 2. Two findings that change what this feature may claim

### 2.1 A third party can manufacture the entire pattern — for gas money

`_normalize_etherscan` builds token movements verbatim from provider rows: `from`, `to`, `value`,
`contractAddress`, `tokenSymbol`, `tokenDecimal` go straight into an `Asset` and a `Movement`, with **no
validation of the token contract** (`chains/evm/adapter.py:396-424`; Tron is identical at
`tron/adapter.py:247-268`). An ERC-20 is free to emit `Transfer` events between addresses that never
signed anything.

So an attacker deploys a worthless token and emits, in alternating transactions,
`Transfer(random → VICTIM, 1e6)` then `Transfer(VICTIM → BinanceHotWallet, 1e6)`. The victim does
nothing and consents to nothing. CipherChain sees receipts, forwards, ~100% ratios, and a single labelled
destination — a textbook deposit-address pattern, entirely fabricated, aimed at a target of the
attacker's choosing.

The adversary scored seven such ledgers through the proposed confidence functions using the *real*
`find_sweep_matches`. All seven qualified; several outranked the genuine case.

**This is disqualifying until fixed, and it is not confined to the new feature** — `sweep@1` and the
obfuscation detectors are already exposed to it today.

**Mitigation:** an asset provenance floor. `deposit-address@1` considers only the chain's **native
asset** plus token contracts on an **explicit allowlist** — the same posture `labels/` and
`chains/bridges.py` already take (CipherChain ships nothing it has not verified). Weaker fallbacks exist
(require the token to appear across many unrelated addresses) but they are heuristics defending a
heuristic.

> ✅ **SHIPPED (Ruling 2).** `analysis/assets.py` + repo-root `assets/verified-assets.json` (10
> contracts across 4 chains, each confirmed on-chain before shipping). The floor is applied wherever
> movements become evidence — detectors *and* the service-endpoint assessor, whose counterparty degree
> is equally forgeable. It governs **evidence, not traversal**: unverified-asset movements are still
> stored and still expand the graph. Default is fail-closed (native-only) if unwired.
> Regression suite: `tests/analysis/test_asset_forgery.py`, including a premise test that the attack
> *does* fire on a trusted asset, so the negative tests can never become vacuous.
> Live delta on a real wallet: **23 findings → 18**; the five suppressed rested on unverified tokens,
> including one service-endpoint inference built partly on forged counterparty degree.

### 2.2 The claim "deposit address *belonging to* Exchange X" is not supportable

Two disjoint populations produce an identical on-chain shape:

- an address **the exchange controls** and sweeps, and
- an address **a customer controls** and habitually empties into their own exchange account.

Nothing in movement shape separates them. The adversary's "ordinary user cashing out" ledger scored
0.69–0.77 — inside the range proposed for a confirmed deposit address.

It is also false on **Tron and Solana**, which CipherChain supports: exchanges there routinely use one shared
intake address plus an off-chain memo/tag identifying the customer. "Maps 1:1 to a subpoenable account"
is simply untrue on those chains.

**The defensible claim is about the relationship, not about control:**

> *"Deposits from this address were credited by <entity>; <entity> can identify the account they were
> credited to. Identifying that account may also require the transaction memo or tag."*

That statement is true for **both** populations, and it is what an investigator can actually act on. It
is also strictly weaker than what the original framing promised — which is the point.

---

## 3. The design

### 3.1 Placement — a sibling of `_assess_service`, never a detector

A new engine method `_assess_deposit_address`, called from `_process_node` immediately after the
`_assess_service` block, before the direction loop.

It earns that spot: `_assess_service` has no `has_processed_sibling` guard, runs once per *node* (so it
can stamp direction), already sits behind the custodial-infrastructure filter, and performs DB reads
plus an in-memory dict lookup only — no provider call, no Class F violation. The join is **address-first
throughout**: while processing the *deposit* address, resolve `forwarded.to_address_id → get_address →
attribute`. Nothing enumerates the hot wallet's payers.

**It must not terminate the branch.** The labelled hot wallet is one hop further on and is strictly
stronger evidence; closing here would suppress the sourced answer in favour of the inference.

Two corrections the critics forced:

- The **supernode guard runs 12 lines later**, so a high-degree address would be attributed before the
  guard fires. The assessor needs its own inbound-degree exclusion (a per-customer deposit address has
  few funders by definition).
- Give it the `BudgetTracker` parameter **now**, even though `@1` makes no provider call — otherwise the
  contract-detection follow-up lands in a method with nowhere to charge.

### 3.2 Confidence — multiplicative, so the bound is a theorem

```
confidence = round(label_confidence × behavioural_strength, 3)
```

Read as `P(this is E's intake) = P(destination really is E) × P(intake | destination is E)`. The product
form makes "strictly below the hot wallet's own confidence" **a theorem rather than a clamp a later edit
can delete**, and it propagates uncertainty correctly: a 0.70 labelpack cannot mint a 0.85 inference.

The additive-plus-clamp alternative was **rejected on measurement**: at a 0.9 label it returned exactly
0.81 for every candidate from 3 sweeps to 2000 — a flat constant, failing constraint 3 outright.

**Gates are pass/fail, never scored** (constraint 4): minimum sweep count ≥ 2; minimum share of outflow
to the destination; minimum exclusivity (sweeps land on *one* collector); native-or-allowlisted asset
only; destination is not a bridge; inbound degree below the supernode range.

**Signals that are scored**, with non-saturating `n/(n+k)` terms so 2 sweeps and 200 sweeps differ:
concentration, repetition, exclusivity.

**Distinct funders is removed from the score.** The adversary showed it inverts the ranking — a
consolidation service with 60 senders scored 0.797 against 0.760 for the textbook genuine case. A high
funder count means *processor or consolidator*, not per-customer intake. It becomes an **exclusion
signal**, not a bonus.

### 3.3 Structural enforcement — where the compiler and the database say no

Constraint 2 asked for structural, not documented. Layered:

**Barrier 0 — close the live hole first (~6 lines, no migration).** Verified against HEAD: today
`Evidence(kind=THIRD_PARTY_CLAIM, source='...', heuristic='deposit-address@1', confidence=0.82)`
**constructs successfully**. An inference can already wear claim clothes. `Evidence.__post_init__` must
refuse a claim carrying a heuristic, and an inference carrying a source. No current producer sets both,
so this is free.

**Barrier 1 — split the type.** Replace `AttributionResult` with a closed sum: `SourcedClaim` (requires
`source`, no heuristic) and `InferredAttribution` (requires `heuristic@version`, `confidence < 1.0`, and
a `via: SourcedClaim` parent). Evidence construction moves **onto the types** as `as_evidence()`, so that
becomes the only place in `src/` naming `EvidenceKind.THIRD_PARTY_CLAIM`. `inferred.source` becomes a
mypy error.

**Barrier 2 — cap provenance depth at 1.** `deposit-address@1` requires the destination's attribution to
have `basis == SOURCED_CLAIM`. Without this, a sweep into an address itself *inferred* to be a deposit
address mints a further inference, and confidence decays multiplicatively through a self-amplifying
loop. One predicate, checked in one place.

**Barrier 3 — an inference must never close an objective.** `engine.py:257` currently takes the first
`vasp` result regardless of origin. Once a cache exists, a persisted inference would terminate the
branch and be rendered "nearest previous VASP: X" as a sourced claim. Selection for *termination* must
require `SOURCED_CLAIM`. Also: `store.py:39` sorts by confidence alone, so a scaled inference can
outrank a real label — ordering must become **basis-first, then confidence**.

**The hole the critics found in this design:** `as_evidence(about=...)` was specified with a *default*,
so the smallest laundering change is deleting one keyword argument — no compiler error, no failing test.
`about` must be **required and keyword-only**, or better, never a caller decision at all. And
`EvidenceRow` has no address column, so a persisted claim about the *hot wallet* is indistinguishable
from a claim about the *deposit address*. That needs an `about_address_id` column in the same migration.

### 3.4 Storage — and the invariant this collides with

Constraint 5 ("cached forever, coverage compounds") needs a new `inferred_attributions` table plus a
`CompositeAttributor`. Note the caching trap: `LabelStoreAttributor` drains its source into a dict once
at startup. A DB-backed attributor copying that pattern would compile, pass fixture tests, and **never
see anything written during the run**. It must read through a short-lived session per call.

**This is a third storage plane.** The repo is explicitly two — global immutable facts, per-investigation
overlay. Durable *derived knowledge* is neither, and it is the closest this project has come to becoming
an indexer. It needs an explicit entry in `STORAGE_SCHEMA.md` with its lifetime rule (append-only,
`superseded_at`, survives investigation deletion) and the argument for why it is not indexing
(materialised only for addresses an investigation actually touched, never enumerated). **If that
argument cannot be written convincingly, that is evidence the feature is wrong, not that the doc is.**

**Reproducibility is the sharpest problem, and all four designs deferred it.** "Same inputs, same
versions ⇒ same findings" is frozen. Today's leak only bites when a step function flips (supernode 50,
service 25/25/60). `outflow_share` is **continuous**, so *any* new movement in the store changes a number
stamped on a finding — and store growth is reachable without re-running anything, because
`store_movements` writes both endpoints of every movement.

The fix is `knowledge_as_of`: a `TIMESTAMPTZ NOT NULL` on `investigations` beside `engine_version` and
`ruleset_version`, passed to the `before=`/`after=` arguments that already exist and are never used, with
every inference read scoped to `inferred_at <= knowledge_as_of`. **It must land before the store, not
beside it.** Until it does, confidence inputs must come only from the movements *this run fetched*.

### 3.5 Two pre-existing defects this work sits on

Both are in shipped code and affect `sweep@1` today:

- **Disjoint measurement windows.** `movements_to_address` returns **newest-first**;
  `movements_from_address` returns **earliest-first** (`repositories.py:230-252`), both capped at 500. On
  a busy address the assessor holds the 500 *newest* receipts and the 500 *oldest* sends — windows that
  may not overlap at all.
- **Sweep pairing depends on DB insertion order.** Tie-breaking uses `MovementRow.id`, a database
  Identity, and the `consumed` set makes each choice affect all later ones. The same movements ingested
  in a different order can yield different matches — a reproducibility violation in current code.

---

## 4. The hard prerequisite

**CipherChain currently knows zero exchanges.** Measured from the built attributor: 975 records — 905
`sanctioned`, 70 `mixer`, **0 `vasp`**.

The join fires zero times. `InferredAttribution` requires a `via: SourcedClaim`, so the type is
literally unconstructible against production data. The A/B you asked for would report 0 vs 0 because
both arms are empty.

Worse, `labels.py:129` accepts **any** category string with no allowlist — a pack written `"exchange"`
or `"cex"` loads cleanly, logs a healthy "loaded labelpack (N labels)", and matches nothing. That is
exactly the silent-zero failure Ruling 3's unfinished half was meant to catch.

**Order:** (1) category allowlist with loud warn-and-mark-inert, and tighten `confidence < 1.0`
everywhere (a labelpack at 1.0 currently crashes the engine mid-run — `Evidence` rejects `>= 1.0` for
claims); (2) ship `labels/verified-vasps.json` from exchange **proof-of-reserves disclosures** — first-party,
licence-clean, on-chain verifiable, same methodology as the mixer and bridge registries — with a
deliberate `default_confidence` (0.85–0.9, not the 0.7 default), because the inference is defined
relative to it; (3) then the feature.

---

## 5. Test plan

**Unit** (`tests/analysis/test_deposit.py`, no Postgres): monotonicity in each signal; saturation
(`f(50) == f(200)`); bounds (`< label_confidence` always); each gate rejects independently; the
adversary's seven ledgers as **named regression cases with asserted score ceilings**.

**Engine** (synthetic chain, real Postgres): a genuine deposit address is inferred; a single
coincidental payment is not; a relay sweeping to twelve destinations is not; a consolidation service is
not; the emitted evidence is a `heuristic_inference` that cannot be read back as a claim; the branch is
**not** terminated.

**Storage**: an inference written in investigation A is visible to investigation B and is *still* marked
inferred when re-read; and — once `knowledge_as_of` exists — an investigation re-run after the store
grows produces the **same** confidence.

**Live A/B**, mirroring the internal-traces method (same address, same budgets, feature suppressed then
enabled, compare counts): pick a real address that sweeps into a hot wallet in the new pack, and report
**"N addresses attributed by sourced labels alone" vs "N + M once discovery is wired in"**. This test
cannot run before §4 ships, and saying so now is better than discovering it later.

---

## 6. Decisions needed

1. **Does a confirmed deposit-address inference answer the objective?** If yes, reuse
   `FindingKind.VASP_ENDPOINT` (no migration; `engine.py:487-489` already counts direction-carrying VASP
   findings as answers) at the cost of conflating a labelled endpoint with an inference. If no, it needs
   its own kind plus a migration, and the objective still reports "trace exhausted". **My
   recommendation: its own kind, not answering the objective** — given §2.2, "we know who credited these
   deposits" is genuinely weaker than "we found the VASP", and conflating them puts an inference where
   an investigator reads a sourced answer.
2. **Does §2.2's reframing change whether you want this at all?** The feature no longer promises
   "deposit address of Binance". It promises "Binance credited deposits from this address and can
   identify the account". Still valuable — arguably the most actionable line in a report — but it is not
   what the milestone brief described.
3. **Is `knowledge_as_of` in scope for this milestone**, or does the compounding store wait for it? I
   recommend it lands first; the alternative is knowingly breaking a frozen invariant.
4. **Asset provenance floor:** native-only for `@1`, or native plus an allowlist shipped with the VASP
   pack?
