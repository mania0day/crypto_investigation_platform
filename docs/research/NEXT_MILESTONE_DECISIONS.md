# Next Milestone — Decisions Required

**Status:** ✅ RULINGS ISSUED 2026-08-09 — first round IMPLEMENTED · **Source:** re-verification of all
24 deferred findings against current code (2026-08-09), 12 agents, 5 verifiers + 5 designers + 2
adversarial critics

> **Rulings as issued.** 1 — nearest-first claiming, as a correctness requirement. 2 — stay cheap and
> make incompleteness loud, but treat contract-delivered value as a *bug* rather than a cost trade-off
> and fix it outright. 3 — operator labels go through the same evidence discipline as everything else.
> 4 — add `engine_observation`, because Ruling 2's honesty needs a durable place to live.
>
> **What landed:** all four. See "Resolved by the Ruling 1–4 milestone" in
> [`REVIEW_FINDINGS.md`](REVIEW_FINDINGS.md) for the fix-and-proof table.
> **What did not:** Ruling 3 is only partly implemented — see §4.

---

## 1. What the re-verification found

The 24 deferred findings from the 2026-08-07 review were re-checked line-by-line against today's code,
because the code moved substantially after they were filed (Tron, Solana, bridges, obfuscation
detectors, report-quality pass). Result: **26 findings assessed, 22 still real, 3 partially fixed,
1 already fixed.**

Three were **escalated to critical**. All three share one property, and it is the property that matters
most in a forensic tool: **they make the headline answer wrong without saying so.**

| Finding | Was | Now | Why it escalated |
| --- | --- | --- | --- |
| **Zero-value contract calls invisible** | limitation note | 🔴 critical | Understated. The EVM adapter never requests internal traces at all, so a wallet funded by *any* contract — mixer withdrawal, exchange withdrawal contract, smart-contract wallet, L2 bridge exit — reports "Trail ends here." The mixer label is already loaded and never consulted. |
| **Pagination truncation** | medium | 🔴 critical | Worse than filed. Effective caps are BTC **25 txs**, Solana **25 signatures**, EVM 100+100 — and Tron is unpaginatable by construction. A spam-sprayed address never reaches its real funder. |
| **#4 Node identity excludes direction** | high | 🔴 critical | Any address that both received from and sent to the same exchange answers one objective with a confident "trace exhausted" — about an exchange it already stored, attributed, and printed on the same page. |

Two findings also revealed **invariant violations** not previously filed:

- **Reproducibility is broken.** `engine.py:323-327` reads the global fact store with no investigation
  filter, so the same address yields a different "nearest VASP" against a warm store than a cold one.
- **`charge_api(1)` under-counts.** The EVM adapter makes two provider calls per address but is billed
  one, so the API budget does not mean what it says.

And two real defects **nobody owns**, each assumed away by three separate designs:

- **`add_finding` is not idempotent** — no uniqueness constraint (`tables.py:266-288`), plain INSERT
  (`repositories.py:402-429`). Any re-entry into a node duplicates findings, which an investigator
  reads as independent corroboration. Must be fixed before any resume/re-claim work.
- **`claim_frontier` does not lock** — plain SELECT, no state transition (`repositories.py:345-357`).
  Safe today only because there is exactly one entry point. The recovery work creates a second.

---

## 2. The four decisions

Fourteen designs were produced and attacked by two adversarial critics (correctness lens, scope-and-
principle lens). The critics agreed the eight proposed gates collapse into **four real questions**.
Everything else follows mechanically from these.

### Decision 1 — Claim priority: does the search order match the product's yardstick?

The engine claims frontier nodes **highest-value-share first**. The product's answer is phrased in
**hops** ("nearest previous VASP, 3 hops away"). Those are different orderings, and that mismatch is
the root cause of three findings.

- **(a) Distance-ordered claiming** — claim nearest-first, with value share as the tiebreaker within a
  hop level, and put `direction` into node identity. `hop_distance` then becomes the true minimum *by
  construction*: no relaxation machinery, no BFS repair, and "N hops away" is literally true.
- **(b) Keep value-first** — and pay for hop-relaxation machinery to keep the printed numbers honest.

**Recommendation: (a).** Both critics independently verified the minimality argument and both chose it.
It is the only option that also fixes the direction freeze. It shrinks three other designs and deletes
one entirely. The honest cost: with a small budget, a distant high-value cash-out may never be reached
— but that outcome is *reported* as a partial run, whereas today's failure is silent.

> The frozen `ENGINE_DESIGN.md` says value share "affects *order*, never *coverage*." This is an
> amendment to a frozen doc, which the vision permits on discovery of a real architectural flaw. "A
> goal-directed engine whose claim order is not the goal's order" qualifies.

### Decision 2 — Budget economics: buy completeness, or declare blindness?

Two proposed gates are the same question. Free-tier provider keys are the constraint.

- **Internal traces** (~+50% calls per EVM node): makes contract-delivered native value visible.
- **Pagination** (unbounded, budget-capped): makes histories longer than one page reachable.

**Recommendation: buy both, and fix the meter.** The internal-traces fix was called by both critics the
highest-value single change in the round — the only one that converts a *confidently wrong* headline
into a *correct* one — and it needs no migration and no contract change (`Capability.INTERNAL_TRACES`,
`MovementKind.INTERNAL`, and the CHECK constraint all already exist; the adapter simply never asks).
Pagination is the critical-severity one. Both are governed by the existing API budget, so "how much to
spend" stays a per-investigation knob rather than a hardcoded posture — but `charge_api` must be
corrected first, or the budget silently under-bills.

### Decision 3 — Operator-data posture: strict door, or loud room?

Two designs took opposite stances on operator-supplied labelpacks. They can be reconciled by a
principle rather than a coin-flip:

> **Refuse data that would make a recorded claim unprovenanced. Be loud, but permissive, about data
> that merely goes unused.**

So: a labelpack entry with no `source_date` is a **hard load failure** (it would produce an undated
claim in an evidence record). A labelpack using an unrecognized category is **loaded and loudly
reported as inert** (it produces nothing, so it corrupts nothing) — never silently aliased, because a
guessed `exchange`→`vasp` alias would manufacture "nearest previous VASP: Uniswap Router" at full
confidence.

**Recommendation: adopt the principle above.** Defer the sha256 manifest subsystem — ship the refresh
script and derive the date from the data.

### Decision 4 — A fourth evidence kind?

Terminal findings currently file statements about **CipherChain's own run** ("explored frontier ran dry") as
`ONCHAIN_FACT`, with an address as the reference. That is a tool observation wearing an on-chain-fact
stamp, and the closed taxonomy leaves no honest alternative — `ONCHAIN_FACT` requires refs, and a
Finding requires at least one evidence item.

- **(a) Add `engine_observation`** as a fourth kind in the frozen vision §4 taxonomy, used only for
  statements about the run itself. This *narrows* what `onchain_fact` means, which strengthens the
  taxonomy rather than diluting it.
- **(b) Hold at three kinds** and cite only real transaction hashes, accepting that the
  zero-transactions-examined case has no representable evidence.

**Recommendation: (a), gated honestly.** The original design asked for this on behalf of two producers.
In this round alone, **five** designs queue up to use it. Ask once, at full scope, or the amendment
reads as accretion in hindsight.

---

## 3. Sequence (once the four rulings land)

Derived from the scope critic's ordering, with the correctness critic's blockers folded in.

| Round | Work | Notes |
| --- | --- | --- |
| **0** | The four decisions above | No code moves first |
| **1** | File + fix the two unowned defects (`add_finding` idempotency, atomic `claim_frontier`); attribution verdict function; the chosen taxonomy answer | Unblocks everything; no dependencies |
| **2** | Node identity + claim order | Biggest migration. Makes `hop_distance` mean something |
| **3** | "Nearest" earns its word — **one** scope mechanism owning all qualification; directed path evidence; coverage recording | See the warning below |
| **4** | Session-scoping split + concurrency cap, then lifecycle recovery | Pool deadlock + stale `running` rows |
| **5** | Node-creation budget admission, then internal traces, then inert-data reporting, then bridge qualification | Admission must precede acquisition |

### The warning worth heeding

Four separate designs each proposed appending a caveat sentence to the same 33-line `_vasp_finding`
function. On a busy real address, one finding could end up carrying truncated-history, depth-horizon,
unqueried-internal-transfers, unfollowed-bridge and inert-label caveats simultaneously.

**One design must own a single structured coverage/limits block** — "branches closed without
exploration, and why" — fed uniformly by every source. The others contribute sources, not sentences.
Four qualification systems bolted onto one sentence is exactly the accretion this project is trying to
avoid.

### Explicitly not worth doing in v1

Recording "we truncated our own database query" as a permanent schema column (remove the self-inflicted
limit instead); the sha256 manifest subsystem; a hand-maintained `coverage.py` chain→notes table (the
frozen SDK's `adapter.capabilities()` already declares this — derive it); and the second and third
parallel qualifier mechanisms.

---

## 4. What remains after the first round

**Ruling 3 is half done.** Labelpacks without a `source_date` are now refused at load, because an
undated label would reach an evidence record undated. The other half — operator data that loads
successfully but is *never acted on* (an unrecognized `category` produces nothing) — is still silent.
It must become loud, and must **never** be silently aliased: a guessed `exchange`→`vasp` mapping would
manufacture "nearest previous VASP: Uniswap Router" at full confidence.

**Still open, in the critics' recommended order:**

1. ~~**Deposit-address discovery is not wired up**~~ — **CLOSED 2026-08-10, stopped on evidence.** See
   [`DEPOSIT_ADDRESS_DECISION.md`](DEPOSIT_ADDRESS_DECISION.md). Coverage came from label sourcing
   instead (41,839 attribution records). Five shipped-code defects found during the review are fixed.
2. **`add_finding` has no uniqueness constraint** — harmless today (the detector guard closes the path
   this milestone opened), but it must land *before* any resume or re-claim work, or a recovered run
   duplicates findings that "immutable investigation records" then forbids deleting.
3. **`claim_frontier` does not lock** — a plain `SELECT` with no state transition. Safe only because
   there is exactly one entry point. Whichever lifecycle change lands first must make the claim atomic
   (`UPDATE … RETURNING`) in the same change.
4. **Background-task lifecycle** — dropped `asyncio.create_task` refs, no registry, no shutdown drain,
   no recovery for stale `running` rows. Needs a ruling on whether restart auto-resumes or waits.
5. **Pool connection-holding** — a checkpoint session stays checked out across the full provider
   round-trip while the nested cache opens a second connection from the same pool.
6. **Reproducibility leak** — the engine reads the global fact store unfiltered, so a warm store and a
   cold store can still yield different answers for the same address. Not addressed by this round.
7. **Cross-chain deposit↔payout matching** — crossings are detected and reported, never followed.

---

## 5. What this package deliberately does not do

Every design here makes the engine **more honest or more correct about a search it already performs**.
None changes what the search *is*.

That is the right restraint for this round — but it means that if Decision 2 resolves toward
"declare blindness," the tool will still answer "trace exhausted" for a busy address whose answer sits
one page out of reach, and will simply say so accurately. If that is not acceptable for v1, Decision 2
is the one to answer "buy it" on, and it should be answered **before** Round 3, so the coverage wording
gets written once instead of twice.
