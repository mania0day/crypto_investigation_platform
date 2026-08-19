# Backend Review — Findings & Disposition

> ⚠️ **Re-verified 2026-08-09 — the deferred severities below are STALE.** Every deferred finding was
> re-checked line-by-line against current code. 22 are still real, 3 partially fixed, 1 already fixed,
> and **three escalated to critical**. Rulings 1–4 were issued and **the first round is now
> implemented** — see "Resolved by the Ruling 1–4 milestone" immediately below. Read
> [`NEXT_MILESTONE_DECISIONS.md`](NEXT_MILESTONE_DECISIONS.md) before acting on anything still listed
> as deferred; it carries the current severities and the remaining build sequence.

## Resolved by the Ruling 1–4 milestone (2026-08-09)

| Finding | Was | Fix | Proof |
| --- | --- | --- | --- |
| **Contract-delivered value invisible** | 🔴 critical | EVM adapter now declares and requests `INTERNAL_TRACES`; internal rows normalize to `MovementKind.INTERNAL`, skipping reverted, zero-value and endpoint-less traces. | Fixtures recorded live from a wallet whose `txlist` carries **zero** incoming native value while its internal feed carries 9 transfers from the Tornado 0.1 ETH pool. Live A/B on that wallet: **4 → 8 mixer findings** — the four ETH-denominated pools were previously invisible (the DAI pools always arrived via `tokentx`). `tests/chains/test_internal_traces.py`. |
| **#4 Node identity excludes direction** | 🔴 critical | `direction` added to `uq_nodes_identity`; `get_address_node` is direction-aware; a cycle back to the root records an edge instead of re-admitting the subject. | `test_one_exchange_answers_both_objectives` — withdraw-then-redeposit against one exchange now answers **both** objectives instead of declaring one exhausted. Migration `c4f1a7e9d2b8`. |
| **#3 / "nearest" unconditional** | high | Claim order is now **hop asc, then value share desc** — first-discovery hop is the true minimum by construction, so no relaxation machinery is needed. | `test_frontier_dedup_and_priority_order` inverted; `test_value_share_ranks_within_a_hop_level` proves value still ranks siblings. |
| **#8 Engine tool-state as `ONCHAIN_FACT`** | med | Fourth evidence kind `engine_observation` added (vision §4 amended). Terminal findings no longer carry an address as a fake on-chain ref. | `test_terminal_findings_never_wear_the_onchain_fact_stamp`. Migration `d7b3c02f5a19`. |
| **Pagination truncation** | 🔴 critical | Accepted as a cost-driven limit (Ruling 2), but no longer silent: `nodes.history_truncated` records it per address and every terminal derives one coverage statement by query — durable across resume. | Live run reports "884 transaction(s) examined; 8 address(es) had more history than one page — their older transactions were never read". |
| **depth_horizon miscount** | med | `nodes.terminal_reason` distinguishes the five reasons a node stops, so a VASP found at `max_depth` is no longer counted as cut off by it. | `test_depth_horizon_prevents_deep_expansion` now asserts on coverage evidence. |
| **BTC `next_cursor` after window filter** | low → load-bearing | Cursor now derives from the provider's page, not the filtered list, and advances past the whole page. | `tests/chains/test_btc_pagination.py` — a window that empties a full page still pages on. |
| **Duplicate behavioural findings** (new, introduced by the identity fix) | — | Detectors run once per address per investigation; a both-ways address no longer files its patterns twice. | `test_behavioural_findings_are_not_filed_twice_for_one_address`. |

Not changed, and deliberately: the `api_calls` budget still charges **one unit per address expansion**,
now covering three upstream EVM calls. That is a traversal bound, not a quota bound — quota is protected
by the pool's rate limiter — and the docstring states the real ratio rather than implying parity.

**Source:** adversarial multi-agent review of the CipherChain backend (2026-08-07), 6 review dimensions × independent verification. **42 findings survived adversarial verification.** This document records every one and its disposition. Numbers below are stable references used in code comments (`REVIEW_FINDINGS.md #N`).

**Disposition summary:** 18 fixed now (the critical bug, both security issues, and the high-value correctness/resilience defects reachable in wired paths); 24 deferred with rationale — each is either architecture-level (warrants a design gate under this project's process), or scoped to a code path the v1 engine does not yet exercise. Nothing is silently dropped.

---

## Fixed now (with regression tests)

| # | Sev | Area | Fix |
| --- | --- | --- | --- |
| 1 | **critical** | Movement identity minted from a per-vantage positional counter → cross-vantage DROP/DUPLICATE of transfers | Movements now carry an adapter-supplied **vantage-stable `dedup_key`**; uniqueness is `(transaction_id, dedup_key)`. BTC uses intrinsic vin/vout position; EVM uses a content key identical across the etherscan and RPC dialects. Regression: `tests/storage/test_movement_identity.py`. |
| 5 | high | ERC-20 amount decoded over the whole `data` field → wrong value / NUMERIC overflow DoS | `_erc20_amount` decodes **exactly one 32-byte word**; non-canonical data (wrong length / non-hex) is skipped. `tests/chains/test_evm_value_fixes.py`. |
| 6 | high | RPC receipt `status == "0x1"` treats pre-Byzantium (no status, has `root`) and `"0x01"` as failed → silently drops value | `_receipt_succeeded` handles missing-status-with-root and zero-padded encodings. Same test file. |
| 7 | high | Forward traversal ordered newest-first → immediate cash-out hops truncated at busy addresses | `movements_from_address` orders **earliest-first**. `tests/test_review_fixes.py`. |
| 2 | high | Resume grants a fresh `api_calls` budget (only node count was seeded) | `BudgetTracker.seed_spent` carries prior counters forward on resume; wall clock stays per-run by design. `tests/test_review_fixes.py`. |
| 9 | high | Circuit-breaker HALF_OPEN probe slot leaked forever (429-during-probe, untranslated errors, cancellation) → provider wedged until restart | HALF_OPEN releases a stale probe slot after `reset_timeout`. `tests/test_review_fixes.py`. |
| 12 | high | Provider API keys written to logs (httpx logs full request URLs with `apikey=`/`dkey=`/key-in-path) | `configure_logging` raises httpx/httpcore/hpack to WARNING. |
| 13 | high | SSRF / path injection: caller address/txid interpolated unencoded into mempool URL path | Every path segment is `urllib.parse.quote(..., safe="")`-encoded. |
| — | med | `EvidenceOut` omitted `source_date` → claims reached API consumers undated | Added `source_date` to the wire model and mapper. |
| — | med | `confidence == 1.0` admissible for inference/claim evidence → a claim could be presented as certainty | Evidence validation now requires inference/claim confidence `< 1.0`. `tests/test_review_fixes.py`. |
| — | med | Missing OFAC data file silently disabled sanctions screening | Logs a WARNING per missing dataset. |
| — | low | Supernode evidence refs built from an unordered set → non-reproducible across processes | Refs are `sorted()`. |
| — | low | `Movement.amount` accepted floats (silent truncation to int) | `__post_init__` requires an `int`. `tests/chains/test_evm_value_fixes.py`. |
| — | low | Sweep `_confidence` divided by `max_delay` with no guard → `ZeroDivisionError` | `find_sweep_matches` validates `max_delay > 0`. `tests/test_review_fixes.py`. |

(Finding 1's fix also resolves the UTXO-halves index-instability variant and reduces the timestamp-divergence surface, which shared its root cause.)

---

## Deferred — architecture-level (need a design gate before change)

These change engine semantics, the frozen evidence taxonomy, or the SDK contract. Under this project's docs-gated process they warrant an RFC/decision rather than an inline fix at commit time.

- **#3 Node metadata never updated on rediscovery** (high) — `hop_distance` isn't lowered when a shorter path is found; `value_share` doesn't accumulate; a depth-terminated node can't reopen. Can miss a nearer VASP and misreport hop distance. Fix needs a deliberate frontier-update policy (and interacts with #4).
- **#4 Node identity excludes `direction`** (high) — an address reachable both backward and forward keeps only its first direction, killing the other objective's trace through it. Fix: add `direction` to node identity (schema) + expand root-discovered nodes per objective. Design + migration.
- **#8 Engine tool-state labeled `ONCHAIN_FACT`** (high) — terminal findings ("frontier ran dry") use `ONCHAIN_FACT` with an address as `refs`. The closed taxonomy has no kind for tool-state observations; the honest fix is a 4th evidence kind (e.g. `ENGINE_OBSERVATION`) — a change to the frozen vision taxonomy, so it needs sign-off.
- **#10 / #11 Background-task lifecycle** (high) — investigations run as dropped `asyncio.create_task` refs with no registry; lifespan shutdown closes the httpx client / DB engine without cancelling them, and there is no startup recovery for stale `running` rows. Needs a task registry + graceful-shutdown + resume-on-startup design (touches API + engine).
- **Pagination truncation** (med) — the engine fetches one history page and ignores `next_cursor`, silently capping every address at its newest ~100 txs. Fix needs a paginate-under-budget loop and an explicit truncation marker.
- **Pool connection-holding** (med) — a checkpoint session stays checked out across the full provider round-trip while the nested cache opens a second connection from the same pool; under high concurrency this can deadlock the pool. Needs a session-scoping redesign (fetch outside the write transaction).
- **`_install` route-extension not idempotent** (med) — a second lifespan cycle double-mounts routes. Tied to the #10/#11 lifespan redesign.
- **Breaker await-between-check-and-record** (med) — the pool awaits between `allow()` and `record_*`, violating the breaker's single-loop assumption under concurrency. #9 mitigates the worst outcome; a full fix needs per-provider locking or a redesigned probe protocol.
- **`resolve_bridge` / cross-chain** — bridge findings (`MIXER_INTERACTION`, `BRIDGE_CROSSING`) are dead enum members; the frozen plan places resolution in `analysis/bridges`, not yet built.

## Deferred — correctness in not-yet-wired paths, or low-severity polish

Real defects, but scoped to code the v1 engine loop does not currently drive (public adapter API used only in tests), or low-impact quality items. Tracked for the relevant future work.

- **#path_tx_hashes undirected BFS** (med) — evidence path can splice mixed-direction edges; overstates the "value path". Fix with a directed, tree-scoped traversal (pairs with #3/#4).
- **Token decimals first-writer-wins** (med) — `get_or_create_asset` never reconciles; RPC-first contact fixes `decimals=0`. No consumer converts by decimals yet; fix is an asset-registry reconciliation (Class C enrichment, not built).
- **BTC `next_cursor` after window filter** (med) — pagination can terminate early when a page is emptied by the window filter. Only reachable via the paginating adapter API, not today's single-page engine.
- **Only highest VASP claim surfaced** (med) — conflicting attributions dropped, contradicting the store's "show every claim" contract. Fix: emit one finding per distinct VASP claim.
- **OFAC snapshot date hand-maintained; `refresh_ofac.py` absent** (med) — refreshing data without editing the constant fabricates provenance. Fix: derive the date from the vendored files + add the refresh script.
- **Chain-agnosticism leak: `0x`-lowercasing in `engine.start`** (low) — per-chain canonicalization in the engine (also duplicated in labels + adapter). Fix: an adapter `canonical_address()` hook on the SDK (contract change).
- **Storage imports `providers.CachedEntry`** (low) — a passive DTO import crosses the storage↔providers package line. Fix: move `CachedEntry` to `core`.
- **claim_frontier doesn't lock** (low) — concurrent resume of the same investigation double-processes a node (findings duplicate). Fix: `FOR UPDATE SKIP LOCKED` + a run guard (pairs with #10/#11).
- **depth_horizon miscount** (low) — the per-child depth guard is unreachable; the "beyond horizon" count is inaccurate. Reporting-only.
- **Root-VASP contradictory terminals** (low) — a root attributed as a VASP still emits "trace exhausted" terminals. Fix: treat a direction-less root VASP as answering all objectives.
- **`max_nodes` not enforced mid-iteration** (low) — a supernode root (guard exempts hop 0) can create ~2×500 nodes before the next check.
- **"nearest" VASP label unconditional** (low) — value-ordered claiming can record a farther endpoint as "nearest". Fix: defer/compare hop distances before labeling.
- **Timestamp divergence** (low), **`movements_for_transaction` 10k cap** (low), **sweep `ONCHAIN_FACT` "forwarded" wording** (low), **`_vasp_finding` refs truncation/overstatement** (low), **non-vasp/sanctioned categories dropped, e.g. mixer** (low), **labelpack `source_date` not required at load** (low) — each documented; fixes are small and will land with the subsystem they belong to.

---

## Demo frontend review (2026-08-07, second pass)

A separate adversarial review covered the demo UI and the API restructure (4 dimensions; 2 of 4 verifiers completed — the api-contract and frontend-security verifiers died on connection errors, so findings in those two dimensions remain **unverified** and were not acted on). 10 findings survived. **All confirmed ones are fixed:**

| Sev | Issue | Fix |
| --- | --- | --- |
| high | A failed new run left the previous run's answer, findings, and green "completed" badge on screen beside the red error | `resetResults()` clears and hides all result panels at the start of every run and on failure |
| high | No generation guard: a slow response from a stopped/previous investigation overwrote the current one's DOM and re-showed the spinner | `runToken` incremented on every start/stop; `refresh()` takes the token and bails on any DOM write if it no longer matches |
| med | Poll never gave up — a stalled run meant a permanent spinner and a permanently disabled Start | `MAX_TICKS` (~10 min) cap that stops cleanly with a "still running" message |
| med | `renderFindings()` rebuilt every card each 1.5s poll, slamming shut any expanded Evidence panel mid-demo | Skip rebuild when the payload signature is unchanged; restore `<details>` open state when it does rebuild |
| med | With no VASP labels the headline was always a muted "No attributed endpoint reached", reading as failure | Reframed as **"Trail ends here"** + the engine's reason (a conclusion, not an absence); `labels/` documents how to add labels for the positive path |
| low | Long unbroken hashes could overflow cards | `overflow-wrap: anywhere` on summary/entity/why/evidence text |
| low | `/` route registered on directory existence, 500ing if `index.html` was absent | Guard on the file, not the directory |
| low | `start_investigation` resolved the engine twice; a shutdown between calls could persist an investigation nothing would run | Resolve once, reuse for both `start()` and `launch()` |

Not fixed: the BTC example address depending on a keyless public provider's availability (verdict *plausible* — external runtime condition; verify before demoing).

Verification: 139 backend tests + a headless render harness that runs the real UI code against live API responses (11 render checks + 6 fix-specific checks, including an XSS/hostile-label test).

---

*Every finding above was code-verified by an independent adversarial pass. The fixed set is covered by regression tests; the deferred set is tracked here so it cannot be silently forgotten.*

---

## Limitation found while testing mixer detection (2026-08-07)

**Zero-value contract calls are invisible to the graph.** `_normalize_etherscan`
emits a native movement only when `value > 0`, so a contract interaction that
transfers no native value produces no movement, no counterparty, and therefore
no frontier node.

Discovered while tracing a Tornado Cash pool: a wallet whose only transactions
were *withdrawal* calls (`value: 0`) yielded no mixer finding, while a wallet
that actually **deposited** 0.1 ETH was detected correctly.

Why this matters forensically: interacting with a mixer, bridge, or sanctioned
contract is significant **even when no native value moves in that transaction**
(withdrawals, approvals, relayer-mediated calls). Today CipherChain sees the deposit
side but not the withdrawal side of contract-mediated flows.

Not fixed here because the right fix is a design decision, not a patch: either
(a) emit a zero-value "interaction" movement kind — which changes the meaning of
the canonical model, since a Movement currently *is* a value movement; or
(b) have adapters surface contract interactions as a separate signal alongside
movements. Both need a gate. Token transfers are unaffected — they arrive via
the `tokentx` feed independently of native value.
