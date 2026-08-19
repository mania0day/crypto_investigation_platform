# Reaching the VASP

**Status:** awaiting approval · drafted 2026-08-13
**Supersedes:** the mixer-terminal rule in `ENGINE_DESIGN.md`; extends the answer
contract in `investigation/answers.py`.

---

## 1. The ruling this implements

> "i dont want to stop at mixer … go foward until VASP … VASP is main and
> important, say weak decision like because of mixer and stuff but i need VASP"
> — 2026-08-13

Three decisions were taken:

| # | Decision | Chosen option |
|---|---|---|
| R1 | A mixer no longer ends a branch | Follow it, **marked speculative** |
| R2 | Budget exhaustion no longer ends a run | Resume route **+** provider rotation **+** a scrape tier |
| R3 | **Each** direction reports a best-effort endpoint | New third answer slot, weakness stated |

Both directions carry equal weight: the VASP the money came **in** from, and the
VASP it went **out** to. They are answered independently and reported
independently — §4.

R1 and R3 change what the engine *asserts*, so they are contract-level and are
written down before any code moves.

### What did not change

The evidence taxonomy. Nothing in this document lets a guess become a name.
`is_named()` still returns true only for `third_party_claim` evidence, and a
mixer-exit branch cannot produce one. This is the constraint every design below
is fitted around, not a preference.

---

## 2. The claim that needs correcting

> "VASP will be there, there is no way no VASP"

Mostly true in practice, and false as a guarantee. The engine must not be built
on it, because a guarantee of an answer is a guarantee of fabrication when the
answer does not exist. Four cases where there is genuinely no VASP:

1. **Issuance.** Trace backward far enough and the coins were mined. There is no
   previous VASP because that is where the money was created.
2. **Not yet cashed out.** A forward trace on funds still sitting in a wallet has
   no next VASP; the event has not happened.
3. **Settled off-ledger.** P2P, OTC, or cash.
4. **A VASP we cannot see.** The trace *did* reach one and we hold no label —
   Solana holds 17,803 Tron labels' worth of nothing: **zero rows**.

Case 4 is the common one, and it is a data problem, not a traversal problem. It
is answered by §6, not by forcing the engine to name someone.

---

## 3. R1 — following a mixer, honestly

### Why "trace through" is not an operation

A mixer severs the deposit→withdrawal link deliberately. Continuing past it means
selecting a withdrawal and asserting it is the subject's. Most of the time that
withdrawal belongs to a stranger. Presented as a normal hop, it produces a
confident path to an exchange account with no connection to the case — the worst
output this system can produce, because nothing in the report marks it.

### What is real: the anonymity set

Tornado Cash pools are **per denomination**, and the shipped label pack already
stores them that way — 0.1 / 1 / 10 / 100 ETH, 100 / 1000 / 10000 / 100000 DAI,
WBTC, USDC, USDT, cDAI tiers. Denomination is the lever:

- A **100 ETH** deposit has few contemporaneous peers. Following it is meaningful.
- A **0.1 ETH** deposit sits in a crowd of tens of thousands. Following it is not.

So the rule is not "follow mixers" or "don't" — it is **follow when the crowd is
small enough to name, and say so when it isn't.**

### The mechanism — and it is NOT symmetric

Both directions must cross mixers, because both answers matter: the VASP the
money came **in** from, and the VASP it went **out** to. But a mixer looks
different from each side, and the candidate set is drawn from the opposite end
of the pool:

| Direction | What happened at the mixer | Candidates are | Window |
|---|---|---|---|
| **Backward** — money in | The address **received** a withdrawal from pool `P` at `T`. We want the funder. | **Deposits** into `P` of the same denomination, **before** `T` | `[T − Δ, T]` |
| **Forward** — money out | The address **sent** a deposit into pool `P` at `T`. We want the cash-out. | **Withdrawals** from `P` of the same denomination, **after** `T` | `[T, T + Δ]` |

Getting this backwards would enumerate the wrong end of the pool and produce
candidates that could not possibly be related — a silent, total failure, since
the output would look identical to a working one. It is pinned by two tests
named for the direction they cover.

The rest of the mechanism is common to both:

1. File `MIXER_INTERACTION` exactly as today. **This does not change.**
2. Enumerate the candidate set for this node's direction, per the table above.
3. `N` = number of candidates = the anonymity set.
4. If `N > MAX_FOLLOW` (default 20): file an `engine_observation` reading
   *"anonymity set too large to follow (N candidates)"* and stop the branch, as
   today. **The honest no.**
5. If `N ≤ MAX_FOLLOW`: create each candidate as a node at `hop+1`, inheriting
   this node's direction, with `discovered_reason="mixer_candidate"` and a new
   node flag `speculative=true`.

Note that the backward window is typically the crowded one: a pool accumulates
deposits over its whole life, so `N` for a backward crossing is often far larger
than for a forward one. Expect backward mixer crossings to refuse more often.
That asymmetry is real and should be reported, not smoothed over.

### Confidence

`confidence = min(0.5, 1 / N)`, and never above 0.5 even when `N == 1`.

The cap is load-bearing and not conservatism for its own sake. A Tornado
withdrawal is not paired to any deposit — a withdrawal inside the window may
correspond to a deposit from months earlier, and the subject may not have
withdrawn at all. `N == 1` therefore means "one candidate", never "certainty".

### The heuristic ladder — strongest first

**Amended 2026-08-13** after reviewing the published literature. The first draft
of this section specified only a timing window, which is the *weakest* of the
known techniques. Commercial tools (MetaSleuth, Elliptic, TRM) advertise
"demixing" without publishing a method; the actual techniques are public, and
the open-source [Tutela](https://github.com/pareto-xyz/tutela-app) implements
five. Ranked by strength, with what each costs us:

| # | Heuristic | Signal | Cost to build |
|---|---|---|---|
| 1 | **Address match** — the same address deposits and withdraws | Decisive | Free — our fact store already holds both sides |
| 2 | **Linked address** — deposit and withdrawal addresses transact outside the mixer | Strong | Free — same data |
| 3 | **Unique gas price** — a manually set, pre-EIP-1559 gas price identical on both transactions | Strong | **Schema change** — see below |
| 4 | **Multi-denomination** — depositing 3×10 ETH + 1×1 ETH leaves a distinctive withdrawal fingerprint | Strong | Moderate — needs per-subject deposit grouping |
| 5 | **FIFO / temporal window** — §3's original mechanism | Weak | Already specified |

Heuristics 1–4 produce a *named candidate*, not a crowd, so they are not subject
to the `1/N` confidence in §3. They file `mixer-exit-<name>@1` at up to **0.8**,
with a summary naming the linking transaction or the matched fingerprint. Only
heuristic 5 falls back to the anonymity-set arithmetic.

Order of evaluation: 1 → 2 → 3 → 4, and only if all fail does the branch fall
through to 5. A run that resolves a mixer by address match should never also
spend budget enumerating a crowd.

**Gas price needs a schema change.** `transactions` currently stores chain, hash,
block number, timestamp and a payload digest — no gas fields. Heuristic 3
requires `gas_price` (and, post-EIP-1559, `max_fee_per_gas` /
`max_priority_fee_per_gas`) on the transaction row. Because raw vendor payloads
are already cached, existing rows can be backfilled by re-parse rather than
re-fetch.

### What this is actually worth

Published linkage rates for Tornado Cash: **5.1–12.6%** of withdrawals from
address reuse plus transactional linkage, with FIFO temporal matching reported to
add a further 15–22 percentage points (that second figure comes from a paper
since withdrawn for reference errors, so treat it as indicative rather than
authoritative).

**So the realistic ceiling for all five stacked is roughly one withdrawal in
three.** Mixer crossing is worth building and is not a general answer. The other
two-thirds are reached by §6 coverage work, not by better mixer arithmetic — and
this is the quantitative reason §2's correction matters.

### Speculation is inherited, and it is sticky

`speculative` propagates to every descendant. Once a branch has crossed a mixer,
everything beyond it is downstream of a guess, and no amount of clean tracing
afterwards launders that. A `speculative` node can never contribute to the
`nearest` or `nearest_named` slots.

### Traversal order: clean paths always win

Frontier claim order becomes:

```
speculative ASC, hop_distance ASC, value_share DESC NULLS LAST, id ASC
```

`speculative` sorts **first** so every clean branch is exhausted before a single
mixer candidate is spent on. This directly serves R3: the real answer, if one
exists, is always found before the guessed one.

Mixer branches also draw from their own sub-budget (`max_mixer_branches`,
default 40) so a pool cannot consume a run.

---

## 4. R3 — always report *something*, and say why it is weak

**Both directions, always, independently.** The product answers two questions and
they fail separately: a run routinely names the exchange the money came **in**
from and not the one it went **out** to, or the reverse. Every rule in this
section applies once per direction, and neither direction's result affects the
other's. A report showing a strong backward answer must still show the weak
forward one beside it, labelled as weak — not omit it, and not let the strong
side imply anything about the weak side.

Today each direction reports two slots: `nearest` and `nearest_named`. A third is
added:

```
best_effort: AnswerEntry | None
```

**Populated only when `nearest` is empty.** If *any* endpoint was found in that
direction, this slot stays null — it exists to fill silence, never to compete.

> **Amended 2026-08-16, by explicit ruling.** The original draft said "only when
> `nearest_named` is empty", which is looser: under it a guess could sit beside a
> real-but-unnamed behavioural endpoint. The implementation took the stricter
> reading and the two disagreed on this line until the call was made in favour of
> the stricter one, which `DirectionAnswer.__post_init__` now enforces
> structurally rather than by convention.
>
> The reasoning: the looser rule buys very little, because a mixer-derived
> candidate can never be named either — so it would add a second unnamed lead
> beside a first, at the cost of a guess appearing next to an answer. The value
> was not worth weakening the property that a guess never shares a row with a
> conclusion.

### Presentation: the name is the headline, the caveat rides with it

**Amended 2026-08-13.** The first draft made `best_effort` a third slot after the
two existing ones, which buries the only name a weak run produces. The consumer
of this report is a regulating body that needs a VASP to act on, and a report
whose headline is "no named endpoint" is not usable by them.

So `best_effort`, when populated, is rendered **in the headline position**, with
its `weakness` string attached inline rather than in a footnote:

```
NEAREST PREVIOUS VASP (money in)
  Kraken 6 · 4 hops · via Tornado 100 ETH pool
  ⚠ 1 of 6 candidates — may belong to an unrelated party
  → verify with KYC before acting
```

The `weakness` string is non-nullable on the wire model, so no renderer can drop
it while keeping the name. The honest terminal finding is still filed and still
returned; what changed is which one a reader sees first.

### Why the marking is retained: it is a priority queue, not a hedge

The operational argument for marking is not caution, it is triage. A report
carrying several leads that all look equally confident forces an investigator to
spend the expensive step — the KYC request to the exchange — equally on all of
them. Marked, the same leads order themselves:

| Order | Lead | Basis | Action |
|---|---|---|---|
| 1 | Binance 14 | signature-verified, clean path | send first |
| 2 | Kraken 6 | via mixer, 1 of 6 | send if 1 returns nothing |
| 3 | unnamed service endpoint | behavioural only | investigate before sending |

No lead is withheld. The label decides sequence, and sequence is what makes a
limited number of subpoenas land well.

There is a second-order reason, recorded once. If weak leads reach a regulating
body indistinguishable from strong ones and a majority come back wrong, that body
discounts the tool's output as a whole — including the signature-verified answers
that were correct. Marking protects the strong answers.

Source order, strongest first:

| Rank | Source | Typical confidence | Stated weakness |
|---|---|---|---|
| 1 | Named VASP on a speculative (post-mixer) branch | ≤ 0.5 | "reached only by following a mixer; may belong to an unrelated party" |
| 2 | Behavioural service endpoint, operator unnamed | 0.55–0.75 | "behaves like a custodial service; we cannot say which one" |
| 3 | Highest-value unexplored frontier address | — | "the trace stopped here with budget remaining unspent" |

Every `best_effort` entry carries a mandatory `weakness` string. There is no code
path that fills the slot without one — same enforcement style as the evidence
constructors.

**The terminal finding still fires.** `best_effort` does not mark an objective
answered; `answered` still requires a `third_party_claim`. The report shows the
honest "no named endpoint" terminal *and* the best available lead, together.
That is precisely the ruling: a weak answer with its weakness stated.

---

## 5. R2 — exhaustion becomes a pause, not an end

Three tiers, tried in order.

### Tier 1 — rotate (no new concepts)

Eight provider credentials are configured. When one vendor's quota is spent, the
pool already fails over on `ProviderRateLimited`; what is missing is that
per-vendor exhaustion should demote that vendor for the rest of the run rather
than being re-tried into the same wall each time.

### Tier 2 — resume (mostly already built)

The engine checkpoints after every address and seeds a resumed run with prior
spend (`seed_nodes`, `seed_spent`). The gap is purely at the edge: **there is no
route to trigger it.** `POST /investigations` is the only write endpoint today.

Add `POST /investigations/{id}/resume`, accepting a fresh budget. Behind the auth
layer, per RFC §6 — it is a write surface.

### Tier 3 — the fetch tier

Approved on 2026-08-13 after the objection below was raised and overruled.

**Objection, recorded once and not re-litigated:** facts obtained against a
site's terms are attackable in exactly the setting this tool exists to serve, and
risk the API keys mid-case.

**Boundaries this is built within:**

- Public pages only. No login, no session reuse, no CAPTCHA handling.
- `robots.txt` respected; identifying user agent.
- Rate: **one request per 5s**, revised from the 2s written here after
  measuring the site on 2026-08-16. 3xpl publishes `x-ratelimit-limit: 25` and
  does not throttle past it — it serves a "Verify you are not a robot" 429 and
  then blocks the host for an hour (`Retry-After: 3600`). The rate is therefore
  a correctness property, not a courtesy: too fast does not mean slow, it means
  no access at all. See `DEFAULT_RATE_PER_SEC` in `explorer_fetch.py`.
- Headless browser: **not built, and not needed for the chosen site.** 3xpl's
  address and transaction pages are server-rendered. The JS-shell explorers
  (mempool.space, blockstream.info) were rejected rather than driven.

**And the part that makes it auditable rather than hidden:** a fetched fact
carries `provider="explorer-fetch:<host>"` in its provenance, like any other
source. It is visible in the evidence trail, it is weighable by a reviewer, and a
report can be filtered to show which conclusions depend on it. The provenance
model already supports this with no changes.

---

## 6. Coverage — the actual answer to "I need a VASP"

When a trace comes back unnamed, it is usually because the label is missing, not
because the money stayed clean. Two efforts, in priority order.

### 6.1 Clustering — the highest-value item in this document

One labelled address should name its **whole cluster**. Two deterministic,
court-defensible techniques:

- **Bitcoin multi-input (co-spend).** Addresses appearing as inputs to the same
  transaction are controlled by one entity — signing requires every input's key.
  Not a heuristic in the weak sense; it is how the ledger works.
- **EVM deposit-address sweeps.** Exchange deposit addresses sweep to a common
  hot wallet. **`sweep@1` already detects this pattern.** Turning existing sweep
  findings into cluster membership is close to free, and 19,027 Bitget and 5,021
  Binance deposit addresses are already in the store to seed from.

Cluster-derived labels arrive `pending`, method `community`, corroborated by the
seed label's source. They name only what the cluster's seed already named.

### 6.2 VASP metadata — turning a name into a filing

**Added 2026-08-13.** The output of this system goes to regulating bodies who act
on it. Today a report says **"Binance"**. A subpoena needs to know *which*
Binance: the legal entity, its jurisdiction, and the law-enforcement request
channel. Large exchanges operate different entities per region, and a request
sent to the wrong one costs weeks.

Per-entity metadata was scoped in `LABEL_INTELLIGENCE.md` §4 — jurisdiction, KYC
regime, source URL and date — and never implemented. It is a new table keyed by
entity rather than by address, since it describes the operator, not the wallet:

| Field | Example | Why |
|---|---|---|
| `entity` | `Binance` | joins to the label's entity stem |
| `legal_entity` | `Binance (Services) Holdings Ltd` | the name that goes on the request |
| `jurisdiction` | `MT` | which authority has reach |
| `kyc_regime` | `full KYC, since 2021-08` | whether records will exist for the period traced |
| `request_channel` | published LE portal URL | where the officer actually files |
| `source`, `source_date` | | same provenance rules as every other claim |

This carries the same evidentiary weight as a label and therefore the same rules:
sourced, dated, never invented. An entity with no metadata reports the name alone
rather than a guessed jurisdiction.

For the stated use — handing a VASP to a regulating body — this is worth more per
hour than any mixer heuristic in §3.

### 6.3 Daily harvester

The lifecycle is built and tested; `reconcile()` simply has no production caller.
A scheduled worker that re-harvests sources, ingests, and runs the reconcile
cycle is the remaining piece. Priority sources: a second exchange proof-of-reserves
(the verifier already handles ECDSA and ed25519), and **any** Solana coverage,
which is currently zero.

---

## 7. Where ML may and may not sit

ML is admitted for the first time, under one rule:

> **ML may propose and prioritise. It may never conclude.**

| Use | Verdict | Why |
|---|---|---|
| Ranking which branch to explore next | **Allowed** | Changes speed, not truth. A mis-ranked branch is explored later, not reported wrongly. |
| Proposing addresses that look like VASPs | **Allowed** | Output lands as `pending`; a trusted source must corroborate before it can name. |
| Extracting entity names from fetched pages | **Allowed** | Same gate: `pending`, needs corroboration. This is the strongest fit — it is a parsing problem. |
| Emitting a label as `active` | **Refused** | Bypasses the lifecycle. |
| Producing evidence that names an operator | **Refused** | Only `third_party_claim` may name, and a model is not a source. |

The rule is not a policy bolted on — the label lifecycle already enforces it.
Anything a model produces arrives `pending` and is inert until something trusted
agrees. No new mechanism is required to keep a model honest here, which is why
this is safe to allow.

What ML must not be used for is the thing it looks most attractive for: deciding
that an address *is* an exchange. A model that outputs "87% exchange" cannot be
cross-examined; `sweep@1` with stated thresholds can. §6.1 gets more VASP
coverage than a classifier would, and survives a courtroom.

---

## 8. Build order

| Step | Item | Depends on |
|---|---|---|
| 1 | `speculative` on nodes + frontier ordering | — |
| 2a | Mixer exit by **address match** and **linked address** (heuristics 1–2) | 1 |
| 2b | `gas_price` columns + backfill, then **unique gas price** (heuristic 3) | 2a |
| 2c | **Multi-denomination** fingerprint (heuristic 4) | 2a |
| 2d | Anonymity-set fallback, `MAX_FOLLOW` (heuristic 5) | 2a |
| 3 | `best_effort` answer slot + mandatory weakness | 2a |
| 4 | Per-vendor exhaustion demotion (tier 1) | — |
| 5 | Auth layer (RFC §6) | — |
| 6 | `POST /investigations/{id}/resume` | 5 |
| 7 | Cluster labels from co-spend + existing sweep findings | — |
| 8 | **VASP metadata table** (§6.2) — jurisdiction, entity, request channel | — |
| 9 | Harvester worker + reconcile scheduler | 5 |
| 10 | Fetch tier | 5, 9 |

Steps 1, 2a and 3 are one coherent change and should land together with the
migration. Steps 7 and 8 are independent of everything else and have the best
value-per-hour in this document for the stated use — 7 finds more VASPs, 8 makes
each one filable. Neither is blocked by the auth layer.

---

## 9. Open questions

1. **Window `Δ`** — 7 days is a guess. Real Tornado withdrawal timing should set
   it. Lower priority now that heuristics 1–4 come first and the window is only
   the fallback.
2. **Anonymity set, properly** — §3 counts withdrawals in a window. The rigorous
   measure is the pool's outstanding deposit count at withdrawal time. The
   simplification is conservative in the right direction (it understates the
   crowd, so it is *more* likely to refuse to follow), but it should be replaced.
2b. **Tutela's licence is unstated** on its repository. Its heuristics are
   described in published work and may be reimplemented from the papers, but its
   code must not be copied until a licence is confirmed — the same rule applied
   to the dashboard reference folder.
3. **Non-Tornado mixers.** Blender.io (45 addresses), ChipMixer, Samourai
   Whirlpool and JoinMarket have no fixed denominations. §3 does not apply to
   them and they keep today's terminal behaviour until a separate model exists.
4. **Does `best_effort` belong in the graph view**, or only in findings?
