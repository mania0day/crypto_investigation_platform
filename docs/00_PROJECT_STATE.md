# CipherChain — Where The Project Actually Stands

**Document:** 00 (not one of the 11 gated design docs) · **Purpose:** the living map — what is decided, what is built, what is still open · **Last verified against code:** 2026-08-10

> Read this first when returning to the project. Docs 01–11 describe what CipherChain *should* be.
> This one describes what it *is* right now.

**Briefing a fresh collaborator** (a new chat session, a second Claude, a human): paste this entire
file, then state the role you want them to play. For an architecture/prompt-engineering peer:
*"You are a principal architect reviewing CipherChain. The document above is the current state. Do not write
implementation code — critique decisions, find the flaw, and challenge scope. The constraints in §3 are
binding."* Sections 6 and 7 carry the open problems and the four rulings currently blocking work — that
is the most useful thing to hand an architect. Section 7 records a major piece of the original plan
being killed on evidence, and is the single best illustration of the standard this project holds.

---

## 1. The product, in one paragraph

Given a blockchain address, CipherChain traces value in both directions and answers one question with
evidence: **what is the nearest previous VASP, and what is the nearest next VASP?** Backward through
funding history, forward toward cash-out. When no attributed endpoint can be reached, that is also an
answer — an explicit terminal finding saying exactly where the trace stopped and why. The engine is the
product; the graph is only its visualization.

---

## 2. The three layers (this is the part that gets confusing)

The project has three separate bodies of work that are easy to conflate. They are at different maturities.

| Layer | What it is | Where it lives | State |
| --- | --- | --- | --- |
| **Architecture** | Decisions about what the system *is* — frozen contracts, taxonomies, invariants | `docs/` | Partly written, partly frozen-by-RFC |
| **Code** | The running backend + demo frontend | `backend/`, `frontend/` | Feature-complete for v1 scope, 266 tests green |
| **Open decisions** | Known defects and gaps whose fix would change a frozen contract | `docs/research/REVIEW_FINDINGS.md` | Catalogued, awaiting rulings |

**The short version of why you feel out of sync:** the original plan was 11 design docs written one at a
time, each approved before the next. In practice only doc 01 was written that way. Docs 02–05 were
effectively *replaced* by four lighter-weight RFCs in `docs/research/`, which were approved and frozen
individually, and implementation proceeded directly from those. Docs 06–11 were never written because
the code they would have specified got built first.

**That was not a process failure — it was a substitution.** But it left no single current map, which is
this document's job.

---

## 3. Architecture — what is DECIDED and FROZEN

These are binding. Changing any of them requires the same deliberation as a vision change.

| Contract | Document | Status |
| --- | --- | --- |
| Mission, core query, evidence posture, non-goals | `docs/01_PROJECT_VISION.md` | ✅ Approved & frozen (§4 amended 2026-08-09: fourth evidence kind) |
| The four rulings and what remains | `docs/research/NEXT_MILESTONE_DECISIONS.md` | ✅ Rulings issued, round 1 built |
| Chain SDK surface (what an adapter must implement) | `docs/research/CHAIN_SDK_INTERFACE.md` | ✅ Frozen — rulings D1–D4 |
| Investigation engine loop and semantics | `docs/research/ENGINE_DESIGN.md` | ✅ Frozen — rulings R1–R3 |
| Database schema (10 tables) | `docs/research/STORAGE_SCHEMA.md` | ✅ Approved — rulings Q1–Q3 |
| Provider capability model (capability over vendor) | `docs/research/CAPABILITY_MATRIX.md` | Research input, informs the above |
| Which providers exist and what they can serve | `docs/research/PROVIDER_INVENTORY.md` | Research input |

### The invariants that constrain every future change

1. **Provider-access invariant** — only chain adapters touch blockchain APIs. Non-negotiable.
2. **Evidence taxonomy is closed** — `onchain_fact` / `heuristic_inference` / `third_party_claim` /
   `engine_observation`. Never conflated. Inference and claim confidence must be `< 1.0`; on-chain facts
   and engine observations carry none at all.
3. **Versioned everything** — heuristics carry a version (`sweep@1`), label datasets carry provenance
   and a source date. A claim without provenance does not ship.
4. **Not-an-indexer** — materialize only what an investigation touches.
5. **Reproducible and resumable** — investigations checkpoint; the same inputs give the same answer.
6. **Never claim legal acceptance** — "evidence-first forensic architecture for investigative
   workflows." Never "court admissible."

### The 11-doc plan, and what really happened

| Planned doc | Reality |
| --- | --- |
| 01 Project Vision | ✅ Written, approved, frozen |
| 02 Architecture | ❌ Never written — **this is the real gap** |
| 03 Investigation Engine | ⟳ Substituted by `ENGINE_DESIGN.md` (frozen) |
| 04 Database | ⟳ Substituted by `STORAGE_SCHEMA.md` (approved) |
| 05 Chain SDK | ⟳ Substituted by `CHAIN_SDK_INTERFACE.md` (frozen) |
| 06 Provider SDK | ❌ Never written — built directly; `CAPABILITY_MATRIX.md` is the nearest thing |
| 07 Intelligence Engine | ❌ Never written — `analysis/` built directly |
| 08 API Spec | ❌ Never written — 5 endpoints exist |
| 09 Frontend | ❌ Never written — demo UI exists |
| 10 Roadmap | ❌ Never written |
| 11 CLAUDE.md | ❌ Never written |

Doc 02 is the only genuinely missing *architecture* document — the others are missing *descriptions of
things that already exist*, which is a much cheaper problem.

---

## 4. Code — what is BUILT and PROVEN

**62 source files, 39 test files, 266 tests passing.** Gates: `ruff` + `ruff format` + `mypy --strict`
+ `pytest`, all green.

### Bounded contexts

| Package | Responsibility | Notable |
| --- | --- | --- |
| `core/` | Canonical models, evidence taxonomy, hashing, settings, errors, logging | The frozen vocabulary — `Movement`, `Evidence`, `Finding`, `Capability` |
| `chains/` | Chain adapters — the *only* code allowed to touch blockchain APIs | 5 chains live; auto-detection by address format |
| `providers/` | Vendor plane: router → cache → rate limiter → retry → breaker, metrics wrapping all | Capability-based, not vendor-based |
| `storage/` | Postgres: immutable fact store + per-investigation overlay | 10 tables, Alembic migrations, idempotent upserts |
| `investigation/` | The goal-directed engine: claim → attribute → guard → expand → re-plan | Checkpointed, resumable, 4 budgets |
| `analysis/` | Attribution (label store) + heuristics + sanctions | Versioned inferences, mandatory provenance |
| `graph/` | Path reconstruction for evidence | Supports the "transaction path" in every answer |
| `api/` | FastAPI — 5 endpoints | Serves the demo frontend at `/` |

### Chains supported

**Bitcoin** (UTXO), **Ethereum** and **Polygon** (EVM family — extra EVM chains are config, not code),
**Solana** (balance-delta paradigm mapped onto UTXO halves so the engine needed zero change), **Tron**
(native TRX + TRC-20 feeds merged, hex→Base58 conversion verified against the USDT contract).

Chain resolution **eliminates on evidence, never chooses**. Candidate chains holding no history are
ruled out, so a single surviving candidate resolves automatically; two chains with genuine history is
a hard stop with a structured 422, and so is one live candidate beside a chain that could not be read.
It never guesses a ledger.

### Detection capability

- **Attribution — 41,839 records.** 14,933 OKX addresses whose signatures this repo verified itself
  (0.9); 25,931 Etherscan exchange tags via eth-labels (0.75, weaker in kind and kept in a separate
  pack); 905 OFAC sanctioned; 70 mixers. By role: 24,068 deposit, 1,863 operational, 15,908 undeclared.
  CipherChain went from naming zero exchanges to naming them routinely.
- **Sanctions are a daily feed now, not a snapshot** (measured and wired 2026-08-18). The harvester
  fetches OFAC's published SDN list every cycle (`harvest/sanctions.py`) — 28.8 MB, 3–6 minutes, 914
  addresses on bitcoin/ethereum/tron/solana — and each claim names the designated party and the
  programs it is listed under rather than a generic "OFAC SDN listed address". This replaces the
  hand-refreshed vendored snapshot in `analysis/sanctions/ofac.py`, under which an address designated
  on Tuesday was not sanctioned here until somebody remembered to re-run a script. The older note
  saying the OFAC downloads "time out from this host" was wrong — it was measured with a 200-second
  ceiling on a six-minute download. **The source fails closed:** a truncated download parses far
  enough to look like a working day (the cut at 53% still yielded 9,959 entries), so the document must
  reach its closing element before a single claim is built, and an incomplete body is refused with
  yesterday's rows left standing. UK OFSI and the EU consolidated list are reachable as well (measured
  the same day); they are unimplemented for want of a parser each, not for want of a route.
- **Asset provenance floor** — a heuristic may only rest on a native asset or a token contract
  verified on-chain before shipping (`assets/`), because a token contract can emit transfers between
  addresses that never signed anything.
- **Registries shipped** — 65 verified bridge contracts, 70 verified mixer addresses. Triple-verified
  against official docs, live OFAC SDN CSV, and DOJ filings, then confirmed on-chain via `eth_getCode`.
  Only `verdict == confirmed` entries shipped.
- **Heuristics** — `sweep@1` (receive-and-forward), plus structural obfuscation detectors:
  peel-chain, distribution (splitter / batch-payout / equal-split), fan-in consolidation, rapid-hop.
  Every detector names its benign lookalike.
- **Mixer contact stops the branch** — tracing through a mixer is de-anonymization, explicitly out of scope.

### The graph view (2026-08-11)

`frontend/index.html` draws the traversal — still one self-contained file, no build step, no
dependencies. Written by hand rather than handed to a graph library, for a reason worth recording:
**a force-directed layout settles differently on every load**, so the same investigation would never
render twice the same way. `hop_distance` and `direction` already impose a total order, so the layout
reads it instead of re-deriving it.

- **Columns are signed hops** — backward hops left of the root, forward hops right. Value therefore
  flows left to right across the whole picture, even though stored edges run in *traversal* order
  (which is backwards to the money on a backward trace). Orientation is taken from the **branch's
  direction**, which is the recorded fact. It was previously inferred by swapping any edge whose
  destination sat at a lower level — the same answer for tree edges, and wrong for every edge that
  returns to a node already placed. Measured on the stored traces: **172 of 2540 edges (6.8%) were
  drawn with the arrow pointing the wrong way**, every one of them a return.
- **Returns are drawn as returns.** An edge that does not advance rightward is money going back to an
  address the trace already placed, and drawing it as progress is a lie about the shape of the flow.
  These arc away from the flow in magenta with a `↩` on the amount: a backwards C into the column
  gutter when both ends share a column, a sag underneath when it travels across columns. Parallel
  returns nest in lanes — on a live trace 29 of 77 drawn edges were returns, and at equal depth they
  fused into one magenta smear that read as a single enormous edge. They are drawn at lower opacity
  than the flow they interrupt: distinct, not dominant. The legend states them as flow ("returns to
  an address already placed at the same or a nearer hop"), never as a verdict — the engine never
  concluded "cycling" or "layering", so the picture must not either.
- **Rows are barycentric** — each column is pulled toward the average height of neighbours already
  placed, sweeping outward from the root, then pushed apart to keep a minimum gap and recentred.
  Centring columns independently put a one-node column beside a fifty-node wall with every edge
  crossing every other.
- **Two independent visual channels.** Border *colour* carries role (root / VASP / sanctioned /
  mixer / bridge / terminal / unattributed); border *style* carries evidence kind — solid for a
  sourced `third_party_claim`, dashed when the caption rests on a heuristic. A VASP named by a label
  and one guessed from behaviour must not look equally certain.
- **Edge labels are placed by collision search**, not by trusting a midpoint: small perpendicular
  offsets first, sliding along the curve before stepping further out, tested against every card and
  every label already placed. Measured on a real 181-edge trace: **0 label-over-card, 0
  label-over-label**, 178 of 181 placed. `ROW_GAP` is the binding constraint — 108→124 took unplaced
  labels from 21% to 1%; column gap barely mattered.
- **Placement order is verified-assets-first.** Order decides which labels get the clearest slots,
  which is emphasis — and emphasis must not be purchasable. Sorting by raw nominal size handed the
  best positions to spam tokens, the same steering vector `BUDGET_EXHAUSTION.md` §3 closed for
  traversal. Amounts are compared with decimals accounted for, unlike the traversal ranking.
- **Unexplored frontier siblings fold into one card.** On the measured trace 92 of 120 nodes were
  frontier leaves carrying no finding — "seen, never looked at" — burying the 28 that had a
  conclusion. They collapse per parent, the card states the count, and *Expand frontier* puts every
  one back.
- **The node budget is spent per hop, not nearest-first.** A flat cap is consumed entirely by the
  first hop the moment that hop fans out wide: on a live trace reaching hops −2…+2, a flat 120
  returned hops −1…+1 and dropped all 202 nodes at hop 2, so the picture lost its depth while the
  run was still going. Each (hop, direction) group is capped separately (default 20), and the
  overall cap is rank-major so it thins every hop rather than deleting the far ones.
- **The graph is not laid out to a prose measure.** The rest of the page is a 1020px reading column;
  a 2400px-wide trace squeezed into it was being blamed on the zoom control. The panel breaks out to
  the viewport, and *Full screen* (or `Esc` to leave) gives it the whole window — a layout change
  only, which re-fits but never redraws.
- **The picture says what it is not showing**: nodes bounded, labels that found no space, addresses
  whose history was truncated (`⋯`), amounts in unverified assets (`⚠`), nodes that ranked 0, and how
  many edges returned rather than advanced.

### API surface

```
GET  /healthz
POST /investigations                       # background or sync
GET  /investigations/{id}                  # status
GET  /investigations/{id}/findings
GET  /investigations/{id}/graph?limit=N    # nodes + edges, readable mid-run
GET  /metrics                              # optional
GET  /                                     # demo frontend
GET  /?investigation={id}                  # reopen a finished trace
GET  /?investigation={id}&full=1           # ... straight into the full-screen graph
```

`graph` returns amounts and value shares as decimal **strings** — smallest-unit sums routinely
exceed 2^53, and a JSON number would be silently rounded by the JavaScript that draws them. Nodes
come back in the engine's own claim order (hop, then value share), so a bounded read keeps the
nearest addresses rather than an arbitrary page, and `node_total` reports the size that was not
returned.

### Running it

```bash
backend/scripts/demo.sh      # postgres + migrations + server on 127.0.0.1:8000
```

---

## 5. Verified working end-to-end

Not just unit-tested — these were run against live chains:

- Real Bitcoin, Ethereum, Solana (25 txs) and Tron (381 txs) traces.
- A real wallet's 0.1 ETH deposit into a real Tornado Cash pool → correct mixer finding.
- Obfuscation detectors firing on real Tron data (splitter across 15 addresses, consolidation across 47).
- A 260-finding real trace that drove a report-quality pass (260 → 164 findings after fixing a
  self-referential-evidence bug and aggregating repeated patterns per address).

---

## 6. Known limits, stated plainly

Re-verified against code on 2026-08-09. Full catalogue in `docs/research/REVIEW_FINDINGS.md`; current
severities and the required rulings in `docs/research/NEXT_MILESTONE_DECISIONS.md`.

### Fixed in the Ruling 1–4 milestone (2026-08-09)

Three findings had been critical — each making the headline answer wrong without saying so. All three
are now closed, with live proof:

- **Contract-delivered value is now visible.** The EVM adapter requests internal traces. On a real
  wallet funded entirely through the Tornado Cash pools, mixer detections went **4 → 8**: the four
  ETH-denominated pools had been invisible (only the DAI ones showed, via token transfers).
- **Node identity includes direction.** An address reached both ways now answers *both* objectives
  instead of declaring one exhausted about an endpoint it had already found.
- **Claiming is nearest-first**, so `hop_distance` is the true minimum by construction and "N hops away"
  is literally true. Value share still ranks siblings within a hop level.
- **Incompleteness is durable and loud.** Pagination limits are accepted as a cost (Ruling 2), but
  `nodes.history_truncated` records them per address and every terminal derives one coverage statement
  by query, so a resumed run cannot print a clean answer over a cut branch.
- **`engine_observation`** joined the evidence taxonomy (vision §4 amended), so statements about
  CipherChain's own run no longer wear the on-chain-fact stamp.

### Known limits of the shipped attribution data

- **Aptos (101,524 rows) and Doge (39,514) are a deliberate skip**, not a forgotten gap. Each needs
  its own signature scheme, and neither is a chain investigators realistically trace VASP flows
  through. EVM, Tron and Bitcoin — the three that matter — are verified and shipped.
- **66 signatures failed verification** across all chains (32 ethereum, 18 polygon, 16 bitcoin) and
  were dropped rather than shipped with a caveat. The Bitcoin 16 carry signatures but no redeem
  script, so they are unverifiable by construction.
- **The published scheme cannot be trusted from documentation.** On EVM the signed payload was the
  keccak *digest*, not the text; on Tron it was the `V2` form, not the header the coin map assigns.
  Both silently recover a *valid but wrong* address for every row. Each chain's scheme was
  established by testing against real published rows — do the same for any chain added later.
- **Proof-of-reserves addresses are reserve wallets, not collection wallets.** Measured: of 36 known
  deposit addresses, **0** swept into a PoR address. The pack answers "these funds reached OKX",
  not "this is where deposits are swept".

### Still open

- **Deposit-address discovery is not wired up** — sweeps are detected and thrown away; nothing joins a
  sweep to the labelled hot wallet it swept into. This is how a tracer gets past its label coverage, and
  it is probably worth more than anything else on this list. See `docs/research/DEPOSIT_ADDRESS_GAP.md`.
- **Reproducibility is still violated** — the engine reads the global fact store unfiltered, so a warm
  store and a cold store can give different answers for the same address.
- **`add_finding` has no uniqueness constraint** and **`claim_frontier` does not lock** — both harmless
  today, both must land before any resume or recovery work.
- **Background-task lifecycle** — no task registry, no shutdown drain, no recovery for stale `running`
  rows.
- **Cross-chain matching** — crossings are detected and reported, never followed.
- **Ruling 3 is half done** — undated labelpacks are now refused; unrecognized categories still load
  silently instead of reporting themselves inert.
- **`AddressRole` reaches the API only as prose.** The engine decides deposit-vs-operational and
  writes it into the VASP finding's *summary* (`engine.py _vasp_finding`), so the graph view matches
  two engine-authored phrases to draw its DEPOSIT / OPERATIONAL chip. That is a deliberate shortcut
  and it is brittle: the proper fix is a structured `role` field on the finding, which is a
  contract-level change and wants a ruling.

**The standing dependency:** attribution coverage. Beyond OFAC and the shipped mixer/bridge registries
there is no broad VASP label set, so most traces honestly end in "trail ends here" rather than naming an
exchange. No engine fix changes this — it is a data problem, and deposit-address discovery is the
cheapest lever on it.

---

## 7. Deposit-address discovery — killed as scoped (2026-08-10)

The original plan's headline differentiator was *behavioural discovery of deposit addresses beyond
public labels*. It was designed twice, reviewed adversarially, and **stopped before implementation**.
Full reasoning and measurements: `docs/research/DEPOSIT_ADDRESS_DECISION.md`.

Three independently disqualifying findings:

- **The value case is circular.** The feature needs its sweep destination to be an already-labelled
  operational VASP — but the engine terminates on *any* `vasp` label and files a sourced
  `VASP_ENDPOINT` that answers the objective. So it can only fire on paths the baseline already
  answers one hop later. Verified directly, not taken on the review's word.
- **The confidence bands do not separate.** Genuine cases 0.423–0.658 (median 0.582); adversarial and
  incidental cases 0.485–0.724 (median 0.593). Six of twelve false positives outscore the genuine
  median. A retail user swapping through the OKX DEX aggregator clears every gate at 0.539 — no
  attacker required. There is no threshold that works.
- **Yield is inversely correlated with need.** Bitget 90% and Binance 70% (the two exchanges that
  already have deposit coverage) versus Coinbase 17% and Kraken 0% (two that have none). The
  measured 98.2% novelty is a recall statement about a *shape*, not evidence the shape occurs where
  coverage is missing.

Also found: a ~$15 decoy — dust sweeps into a real hot wallet while value exits in an unverified
token — scores 0.623, **because the asset provenance floor deletes the attacker's exfil leg before
the assessor sees it**. The floor is right against suppression and backwards against fabrication.

**Condition for revisiting** (`DEPOSIT_ADDRESS_DECISION.md` §6): sample senders to operational
wallets at four or five of the eleven zero-coverage exchanges and measure the deposit-like fraction.
If it tracks Bitget/Binance rather than Kraken/Coinbase, redesign with a **positive collector
allowlist** instead of the role gate and a **flat weak-inference class** instead of a scalar score.
If it tracks Kraken/Coinbase, the sweep signal does not generalise and effort belongs on broader
label sourcing instead.

### Five defects the review found in shipped code — all fixed (`f6d7a28`)

| # | Defect | Was |
| --- | --- | --- |
| 1 | `service-endpoint@1` closed objectives | An inference saying "operator unnamed" suppressed the honest terminal; answering now requires a third-party claim |
| 2 | `role` was a string convention | Now an `AddressRole` enum defaulting to `UNKNOWN`; silence is never read as "operational" |
| 3 | Labels could claim `confidence == 1.0` | Crashed mid-run because `Evidence` rejects it; now strictly `< 1.0` at load |
| 4 | A labelled root terminated before tracing | Answered "this address is Acme" and stopped; now recorded and expanded, matching every other terminator's `hop > 0` guard |
| 5 | Findings could not say which endpoint they reached | Deposit vs operational now stated from `role`, not parsed from prose |

**Ruling recorded on #5:** reaching a customer deposit address *does* answer the objective and still
terminates. It is **more** specific than the collector — it names an account the operator can
identify, where a shared hot wallet names only the operator.

---

## 8. What happens next

Deposit-address discovery is closed (§7), and its five defects are fixed. What remains, in order:

1. **The §6 prior** — sample senders to operational wallets at the zero-coverage exchanges. Cheap,
   and it decides whether behavioural discovery is ever revisited or whether effort goes to broader
   label sourcing instead.
2. **Broader label sourcing** — the lever that actually moved coverage. Non-EVM proof-of-reserves is
   the largest untapped seam: 188,793 published OKX rows are dropped today purely because the
   importer only verifies the EVM signature scheme.
3. **The remaining deferred findings** in `NEXT_MILESTONE_DECISIONS.md` §4 — `knowledge_as_of`
   before any compounding inference store, `add_finding` idempotency, the atomic `claim_frontier`,
   and the background-task lifecycle.

---

## 9. Working agreement

- Docs before code for anything that changes a frozen contract. The four-question feature gate applies
  to every feature: why does it exist, where does it belong, does it violate a principle, what evidence
  would prove it works.
- Build order is the owner's call. Propose resequencing; wait for approval.
- After every code change, run the full gates unprompted — `ruff` + `mypy --strict` + `pytest` — and fix
  what fails before reporting.
- Push-back is invited. If something creates technical debt, say so before it gets coded.

---

*This document is descriptive, not binding. When it disagrees with the code, the code is right and this
document is stale — re-verify and update it.*
