# Deposit-Address Discovery — Recommendation: do not build it as scoped

**Status:** ⏸ AWAITING RULING · no code written · **Source:** 8-agent design pass against HEAD
`8606c04` (3 verifiers, 2 designers + 1 that died on a connection error, 2 adversarial critics),
2026-08-10

> **Both critics returned `needs_revision`, and the value critic's verdict is blunter than that:
> "Win 1 captured the answers, and this feature as designed cannot add one."** I verified the
> load-bearing claim myself. The recommendation is to stop, and to spend the effort on five
> defects this review surfaced instead — three of which are live in shipped code right now.

---

## 1. The value case is circular

The feature requires the sweep destination to be an **already-labelled operational VASP** — that is
what makes the inference safe at all (it caps provenance depth so an inference never rests on
another inference).

But `engine.py:274-284` terminates on **any** `category='vasp'` result and files a *sourced*
`VASP_ENDPOINT`, which `_finish_completed` counts as answering the objective
(`engine.py:541-543`). So on the path `root → … → D → H`, when the engine expands the candidate
deposit address `D`, it creates node `H`, `H` is labelled, and the objective is answered **at 0.75,
from a sourced claim, one hop later** — with or without the feature.

The inference about `D` is therefore an *annotation on an already-answered path*, never an
additional answer. The A/B as specified would report **A == B on objectives answered**.

The fallback case — "answers gained when the budget stops before `H` is claimed" — does not exist
either. At `max_depth=1` the depth check returns **before** `address_history`, so `D` is never
expanded, `D → H` never enters the store, and the assessor never runs. That arm produces no
findings in either condition.

## 2. The confidence function cannot separate genuine from benign

The adversary transcribed the proposed formula verbatim and reproduced the design's own worked rows
to ±0.001, so the numbers below are the design's arithmetic:

| population | band | median |
| --- | --- | --- |
| genuine deposit addresses (design's own rows) | 0.423 – 0.658 | 0.582 |
| incidental + adversarial scenarios | 0.485 – 0.724 | 0.593 |

**Six of twelve false positives score above the genuine median. There is no threshold that keeps
the true positives and drops these.** The cheapest false positive needs no adversary at all: a
retail user swapping through the **OKX DEX aggregator** clears every gate at 0.539.

The root cause is that the destination gate does not mean what it says. Of the 1,863 addresses
tagged `role='operational'`, **392 are structurally incapable of crediting a customer account** —
DEX routers, token contracts, public charity donation addresses, merchant-payment endpoints, gas
funders, deployers, multisigs and cold wallets.

## 3. A deliberate false trail costs about $15 — and our own security fix helps it

Twenty dust ETH sweeps into Binance 14 (~$12 plus gas), while the real value leaves in an
unverified token. **The asset provenance floor deletes the exfil leg before the assessor sees it**,
so concentration, sweep count and destination-spread all read as a clean single-destination
address. Score: **0.623**, above the genuine median.

The floor is correct against *suppression* and exactly backwards against *fabrication*. That is not
an argument against the floor — it is an argument that this heuristic is the wrong thing to build
on top of it.

## 4. Yield is concentrated where coverage already exists

This is the finding that turns the headline around. Measured deposit-like yield by exchange:

| exchange | deposit labels already held | measured yield |
| --- | --- | --- |
| Bitget | 19,027 | **90%** |
| Binance | 5,021 | **70%** |
| Coinbase | 0 | 17% |
| Kraken | 0 | **0%** |

Yield is highest at exactly the two exchanges that already have deposit coverage, and collapses at
two of the eleven that have none. The measured **98.2% novelty** figure is a *recall statement
about a shape* — it is not evidence that the shape occurs where coverage is missing. "For 11 of 15
exchanges every attribution is new by construction" is true and misleading: new, and measured at
0–17%.

---

## 5. What to build instead — five defects this review surfaced

Three are **live in shipped code today**, independent of any new feature.

1. **A labelled deposit address terminates the trace.** Verified: all 24,068 `role='deposit'`
   records carry `category='vasp'`, so `engine.py:274` fires and the engine reports *"nearest next
   VASP: Bitget (deposit address)"* and stops. Reaching a customer intake address does mean the
   funds reached Bitget — but the collector one hop on is the stronger, more subpoenable endpoint,
   and the trace never gets there. Needs a deliberate ruling, not a silent behaviour inherited from
   Win 1.
2. **A behavioural inference silently suppresses the honest terminal.** `service-endpoint@1` emits
   `VASP_ENDPOINT` with a direction at ≤0.75 confidence, and `answered` counts any
   direction-carrying `VASP_ENDPOINT` (`engine.py:541-543`). So an *unnamed-operator guess* closes
   the objective and suppresses "trace exhausted". Live at HEAD, no cache, no new feature.
3. **`role` is never loaded.** `LabelRecord` and `AttributionResult` carry no `role`, so the packs'
   `deposit` / `operational` distinction is invisible to the engine — it exists only in the JSON
   and in the entity string. Any gate on it is currently unexpressible.
4. **VASP termination has no `hop_distance > 0` guard**, unlike `_assess_service` and the supernode
   guard, so a labelled root terminates immediately.
5. **`labels.py` still admits `confidence == 1.0`**, which `Evidence` then rejects mid-run for a
   third-party claim — a latent crash on the attribution path.

Also worth correcting: **R5's rationale is empirically wrong.** Total and novel attributions differ
by under 2% (1 of 55 sampled candidates was already tagged), so no de-duplication machinery is
needed. Report the novel count because it is the honest number, not because the total would be
inflated — otherwise a later reader builds infrastructure to solve a 1.8% problem.

---

## 6a. The prior was run (2026-08-10) — and it closes the question against reviving

Measured directly, with a stated definition: a sender is *deposit-like* when it received value,
forwarded ≥90% of its outbound value to one operational wallet, and paid ≤2 distinct destinations.
Probe wallets were filtered to a **positive collector shape** (`<Entity> <n>` or a named hot wallet,
excluding Swap/Deployer/Charity/Router/Cold/MultiSig), per the adversary's recommendation.

| exchange | wallet | senders checked | deposit-like |
| --- | --- | --- | --- |
| Gemini | Gemini 2 | 14 | **79%** |
| Crypto.com | Crypto.com 38 | 11 | **73%** |
| Bitget *(control)* | Bitget 19 | 5 | 40% |
| HTX | HTX 152 | 14 | 29% |
| MEXC | MEXC 3 | 12 | 25% |
| Bitfinex | Bitfinex 5 | 14 | 14% |
| Binance *(control)* | Binance 105 | 14 | **0%** |
| Coinbase | Coinbase 8 | 14 | 0% |
| Kraken | Kraken 2 | 14 | 0% |

**The controls did not reproduce**, which is the finding. The earlier pass reported Bitget 90% and
Binance 70%; measured independently here they are 40% and 0%. Re-probing across several wallets per
exchange shows why: Gemini reads 79% on one wallet, 31% on another and 0% on two more; Crypto.com
reads 73% on one and 0% on another. **Binance reads 0% across four different wallets** (n = 14, 13,
14, 1) despite holding 5,021 labelled deposit addresses.

That last row is decisive. Binance demonstrably *has* thousands of customer deposit addresses — they
simply do not sweep into the wallets a collector-shaped name filter selects. The earlier 36/36
measurement worked because it ran **forward from known deposit addresses**; it never had to guess
which wallet was the collector. Running the same question backwards requires knowing which of an
exchange's operational wallets is the deposit collector — **and at an exchange with no deposit
labels, that is exactly the knowledge we do not have.**

So the feature faces a *second* circularity, independent of §1: it needs a collector seed to aim at,
and the only reliable way to identify a collector is to observe known deposit addresses sweeping into
it. Where deposit labels exist, the feature is redundant; where they do not, the seed cannot be
identified.

**Conclusion: the §6 condition is not met, and the recommendation to stop stands.** The result does
soften §4's framing — the deposit-address *shape* does occur at some zero-coverage exchanges — but
shape occurrence was never the binding constraint. §1 (circular value) and §2 (non-separating
confidence bands) were each independently disqualifying and are untouched by this measurement.

## 6b. What would change this recommendation

The cheap prior, before any code: sample senders to the operational wallets of four or five of the
**eleven zero-coverage exchanges** (HTX, Crypto.com, Bitstamp, Bybit, Bitfinex) and measure the
deposit-like fraction. If it tracks Bitget/Binance (70–90%) rather than Kraken/Coinbase (0–17%),
the feature has a real target population outside the covered set and this recommendation should be
revisited — with the destination gate replaced by a positive collector allowlist, and the scalar
confidence replaced by a flat weak-inference class, since the measured bands do not rank.

If it tracks Kraken/Coinbase, the honest conclusion stands: Win 1 took the value, and the remaining
effort belongs on the five defects above.

---

*One design agent (`mechanism`) died on a connection error, so the join's exact call shape is not
written up here. Given the recommendation is to stop, that gap does not block the decision — it
would need to be redone if §6's prior comes back positive.*
