# Investigation Engine — Design RFC

**Status:** ✅ FROZEN (approved 2026-08-07). Rulings: R1 — attribution port + NullAttributor this milestone. R2 — sanctioned addresses: record + continue. R3 — frontier exhausted without VASP = `completed` + explicit terminal finding; `partial` reserved for budget exhaustion.
**Scope (frozen by vision + Phase 5):** exactly two objectives — `find_prev_vasp` (backward) and `find_next_vasp` (forward). Goal-directed expansion only; four budgets; supernode guard; checkpointed, resumable, explicit terminal findings.

## 1. The loop

One investigation = repeated iterations of *claim → attribute → guard → expand → re-plan*, each iteration committed as a checkpoint (crash-safe, resumable):

1. **Budget check** — `api_calls`, `seconds` (per-run wall clock), `max_depth`, `max_nodes`. Exhaustion ⇒ status `partial` + a terminal finding naming the budget and the unexplored frontier size. Silent truncation is forbidden (vision principle 9).
2. **Claim** the highest-priority frontier node: `value_share DESC NULLS LAST, hop ASC` (the schema's checkpointed frontier).
3. **Attribute first, fetch second.** The attribution port is consulted *before* any API spend. A VASP label ⇒ `vasp_endpoint` finding (evidence: the label claim + the on-chain path from the root), node `terminal`, branch closed. Objective achieved at zero marginal cost.
4. **Supernode guard.** If the node's counterparty count exceeds a threshold (config, default 50) and it isn't the root ⇒ `terminal` finding ("high-degree address not expanded through"), no expansion.
5. **Expand** via the chain adapter (`address_history` → `normalize` → fact store). Movements are global facts: a later investigation touching the same address pays nothing.
6. **Re-plan.** Counterparties derived *uniformly from stored movements* (no chain branching): account movements carry both endpoints; UTXO halves resolve through the joining tx's opposite halves. Each counterparty becomes a frontier node (hop+1, objective direction, value share) unless `max_depth` prunes it; edges record which movement links them.
7. **Finish.** Frontier empty ⇒ `completed`; any objective without a VASP finding gets an explicit terminal finding ("trace exhausted, no attributed endpoint, N transactions examined").

## 2. Decomposition (no God class)

```
investigation/
  objectives.py    Objective enum → trace direction
  budgets.py       Budgets + BudgetTracker (pure, injectable clock)
  attribution.py   Attributor PORT (protocol) + AttributionResult — implemented by analysis/ in Phase 6
  frontier.py      counterparty derivation from stored movements (both paradigms)
  engine.py        orchestration loop only
graph/
  paths.py         path reconstruction over investigation edges (evidence trails)
```

The engine consumes: `ChainRegistry`, repositories, and the `Attributor` port. It never touches providers (vision principle 1 — structurally enforced: it has no pool reference).

## 3. Semantics worth pinning

- **Priority across assets (v1 limitation, stated):** `value_share` ranks by raw smallest-unit amounts, which is not comparable across assets. It affects *order*, never *coverage* — everything within budget is still explored. Price-normalized ranking is enrichment, later.
- **Time-respecting windows (deferred, stated):** adapters accept `TimeWindow`, but v1 expansion does not yet propagate per-branch windows. The core-query semantics tighten when analysis lands.
- **API accounting (v1 approximation, stated):** `api_calls` charges 1 per history fetch; exact per-investigation pool accounting arrives when the pool gains request tagging.
- **Versions:** every investigation records `engine_version` + `ruleset_version` (reproducibility, vision §4).

## 4. Rulings needed

**R1 — Attribution scope for this milestone.** Recommendation: ship the *port* + a `NullAttributor` (engine finds no VASPs until Phase 6 lands real label data — truthful, and the milestone stays small). Alternative: pull a minimal OFAC loader forward into this milestone.

**R2 — Sanctioned addresses.** Recommendation: record a `sanctioned_address` finding and *continue* expanding — funds moving *through* a sanctioned address are exactly what the trace must follow. Alternative: treat as terminal like VASPs.

**R3 — "Frontier exhausted, no VASP found" status.** Recommendation: `completed` + explicit terminal finding — the investigation ran to its natural end and "no attributed endpoint within horizon" IS the answer. `partial` stays reserved for budget exhaustion. Alternative: mark these `partial` too.
