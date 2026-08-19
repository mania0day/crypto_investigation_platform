# CipherChain holds the answer and does not read it

**Status:** ✅ **(a) FIXED and confirmed live — see §7** · measured 2026-08-11 on four live traces ·
**(b) still open, and now acute — see §8**

---

## 1. What was checked, and why

A live trace (`3dfdd721`) returned four `vasp_endpoint` findings, all of them
`service endpoint — operator unnamed` at 58–62% confidence. That is a behavioural guess. On a chain
where we ship **25,931 Etherscan exchange labels, 12,889 infrastructure labels and 14,863
signature-verified VASP addresses**, four unnamed guesses is either honest coverage or a bug in the
answer. So: are those four addresses actually unlabelled?

**They are.** All four are in no pack we ship:

| address | in labelpacks |
| --- | --- |
| `0x0b40fa6b…1bd58c37` | no |
| `0x7d4a5d64…d925e621` | no |
| `0xb300000b…19c7028d` | no |
| `0x8f10b468…d113f996` | no |

So the headline answer was honest. The check that was meant to take twenty minutes and find nothing
found something else on the way.

## 2. The finding

Of the 308 addresses that trace touched, 18 carry a label. Three of them are **sourced VASP
labels**:

| address | label | hop | direction | node state |
| --- | --- | --- | --- | --- |
| `0x28c6c062…3bf21d60` | **Binance (operational address)** | 2 | backward | `frontier` |
| `0xa9d1e08c…fb81d3e43` | Coinbase (operational address) | 2 | backward | `frontier` |
| `0xb5d85cbf…2f43f511` | Coinbase (operational address) | 2 | backward | `frontier` |

`frontier` means **discovered but never processed**. CipherChain found Binance's operational address,
already held a 0.9-confidence sourced label for it, and never looked — then reported a 61%
behavioural guess as its answer to "where did the funds come from".

This is not the nearest-first rule misfiring. Nearest-first is working: the guesses are at hop 1, the
labels at hop 2, and hop 1 is genuinely nearer. The problem is upstream of ordering.

## 3. Root cause: a free lookup priced as an expensive one

`LabelStoreAttributor.attribute()` is a **dict lookup over an in-memory index**
(`analysis/attribution/store.py`). No provider call, no I/O, no budget.

But it is only ever called from `_process_node` (`engine.py:251`), which runs only on a node claimed
off the frontier — and claiming a node means **expanding** it, which on EVM costs three provider
calls and one `api_calls` charge.

So a zero-cost question is gated behind an expensive one. When the budget dies, every labelled
address already sitting in the frontier dies unread — including the ones that answer the objective.

## 4. It is not one trace

Every Ethereum trace measured shows it. `vasp findings` is what the run reported; `labelled VASPs
never attributed` is what it was already holding and did not read.

| trace | nodes | vasp findings | labelled VASPs never attributed | nearest such label |
| --- | --- | --- | --- | --- |
| `3dfdd721` (`0x1231deb6…`) | 334 | 4 (all unnamed guesses) | 3 — Binance, Coinbase ×2 | hop 2 |
| `0x6bd0b42f…` | 205 | 9 | 6 — Binance ×2, KuCoin ×2, OKX, Coinbase | hop 2 |
| `0x388c818c…` | 167 | **1** | **7** — Coinbase, Binance ×4, Crypto.com | **hop 1** |
| `3PeVz6zC…` (bitcoin) | 49 | 0 | 0 | — |

The third row is the one to look at. **Coinbase and Binance sat at hop 1 backward — one hop from the
root — discovered, labelled, and never read**, while the trace reported a single VASP finding.

Bitcoin shows zero because its label coverage is thin (3,313 addresses), not because the mechanism
differs.

## 5. What a fix would change

Attribute a counterparty when it is **added to the frontier**, not when it is expanded. The evidence
shape is unchanged and already supported:

- `onchain_fact` — the movement that reached the address (we have it; it is the edge)
- `third_party_claim` — the label, with its source and date (we have it; it is free)

Answering "these funds went to Binance" does not require reading Binance's own history. It requires
the transfer and the label, both already in hand.

Expected effect on the measured traces: the `0x388c818c` run would answer **Coinbase / Binance at
hop 1, sourced, 0.9** instead of one guess; `3dfdd721` would answer **Binance at hop 2, sourced,
0.9** instead of `operator unnamed` at 0.61. At **zero additional provider calls.**

## 6. The ruling this needs

Two questions, and both change what "answering the objective" means, so neither is mine to settle.

**(a) Should attribution run at discovery?** It makes the engine able to answer with an address it
never expanded. Argued for above; the cost is that a VASP finding no longer implies CipherChain read that
address's history. If yes, the finding should say so — an `engine_observation` noting the endpoint
was identified by label without expansion would keep that visible.

**(b) When a sourced label at hop N+1 competes with a behavioural inference at hop N, which is the
answer?** Today nearest-first decides and the guess wins. There is precedent the other way:
`5252072` established that *a sourced label beats a behavioural guess* — but that was two claims
about the **same** address, not a claim at a nearer hop against a claim at a further one.

The honest options:

1. **Nearest wins, unchanged.** Report the hop-1 guess as the answer and the hop-2 sourced label as
   a second finding. Defensible: "nearest" is the stated objective.
2. **Sourced beats inferred, within a hop tolerance.** A named exchange one hop further out is the
   better answer to "where did the funds come from" than an unnamed guess. Needs a stated tolerance,
   or it stops being "nearest" at all.
3. **Report both, rank neither.** Show "nearest endpoint" and "nearest *named* endpoint" as separate
   answers. Most honest, and it makes the trade visible to the investigator instead of resolving it
   for them.

Option 3 is the recommendation: the two are genuinely different questions, and the tool's whole
posture is to show the reader what it knows rather than to pick for them. But (a) is worth doing
regardless of how (b) is settled — reading a label we already hold costs nothing, and not reading it
is indefensible under any of the three.

---

## 7. (a) shipped and confirmed (2026-08-11)

`_attribute_on_discovery` runs the label store against every counterparty **the moment it is created
as a frontier node**, immediately after its edge is stored — the edge must exist first, or the
finding's value-path evidence reconstructs to empty.

Only what the attributor alone can decide is decided there: a **VASP** or **MIXER** label files the
same finding, from the same builder, with the same evidence, and closes the branch. Everything
resting on the address's own history — the service-endpoint inference, the obfuscation detectors,
the supernode guard — stays in `_process_node`, where that history is actually read. Nothing about
the evidence taxonomy, `direction`, `hop_distance` or the `answered` gate changed: a discovery-time
finding carries a `third_party_claim`, so it satisfies the gate exactly as before.

Attribution runs **only for a node just created**, so each is attributed exactly once, and a labelled
node is closed the moment it is named — so `_process_node` can never claim it and file the claim a
second time.

### Re-run of every trace in §4, same budgets

| trace | before | after |
| --- | --- | --- |
| `0x1231deb6…` (the reported one) | 4 findings, **0 sourced** | 4 findings, **2 sourced** — Binance, Bitget |
| `0x388c818c…` | 1 finding, **0 sourced** | 14 findings, **4 sourced** — MEXC, Binance, Coinbase, Gate.io |
| `0x6bd0b42f…` | 9 findings, 8 sourced | 15 findings, **14 sourced** — adds OKX at **0.90**, KuCoin ×2 |

The two runs that had never named a single exchange now name four and two. Every one of those
answers came from a label already on disk, at **zero additional provider calls** — the budgets are
identical and `api_calls` spent is unchanged (25, 25, 20).

Permanent regression in `tests/investigation/test_engine.py`: a ledger where the labelled exchange
funds the root with the *smallest* amount, so nearest-first ranks it last and a one-call budget can
never claim it. Asserts the exchange is still named at 0.9 with both evidence kinds, that its value
path survived, and that `adapter.history_calls == [ROOT]` — the answer cost nothing. Verified to
fail without the fix.

### Independence from the traversal-ranking fix

Asked and checked: the two do not interact. `BUDGET_EXHAUSTION.md` §5 governs `value_share`, which
orders the frontier **for claiming**. This reads a property of nodes that have **not** been claimed,
at creation time, before any ordering applies. They are orthogonal.

There is a one-way benefit worth naming, though: the steering vector that fix closed could
previously bury a labelled VASP so far down the queue it was never claimed at all — which, before
this change, meant never read. A label is now read regardless of where spam pushes its node in the
ranking, so the attack can no longer hide a known exchange, only delay its expansion.

---

## 8. (b) is now acute, and still unruled

Before this change most traces produced *only* unnamed guesses, so nothing competed. Now sourced
labels and behavioural guesses coexist in the same direction — `0x1231deb6…` has 2 of each backward,
`0x388c818c…` has 4 sourced against 10 guesses — and something has to pick the headline.

Nothing does, on purpose. `renderAnswer` takes the **first** `vasp_endpoint` finding for the
direction, and `list_findings` orders by insertion, so the headline is decided by traversal order.

On all four re-runs it happened to land on a sourced answer. That is luck, not a rule: a hop-1
service-endpoint inference recorded before a hop-2 label would take the headline, and the report
would show a 0.6 guess while a named exchange sat below it. **The outcome is currently right and the
mechanism is arbitrary**, which is exactly the combination that stops being right without warning.

§6(b) still needs its ruling. The three options stand; option 3 (report "nearest endpoint" and
"nearest *named* endpoint" as separate answers) remains the recommendation.
