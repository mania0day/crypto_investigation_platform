# Tracked Gap — Deposit-Address Discovery Is Not Wired Up

**Status:** ⛔ CLOSED — the feature this gap describes was designed twice and **stopped before implementation**. Read [`DEPOSIT_ADDRESS_DECISION.md`](DEPOSIT_ADDRESS_DECISION.md) first; it carries the measurements and the condition for revisiting. The design attempt is [`DEPOSIT_ADDRESS_DESIGN.md`](DEPOSIT_ADDRESS_DESIGN.md).

> ⚠️ **Corrections after re-verification (2026-08-09).** The diagnosis below holds; two of its
> conclusions do not.
>
> 1. **"First place it would need to be consumed: `_run_detectors`" is WRONG.** That function now
>    early-returns on `has_processed_sibling` (`engine.py:425-428`), so an assessment placed there fires
>    for at most one direction per address — and zero times for an address whose sibling terminated
>    early. A deposit heuristic also cannot be a `Detector` at all: the signature grants it no attributor
>    and no repository. The join belongs beside `_assess_service`, at engine level.
> 2. **The heuristic as framed here is not safe to ship.** A third party can manufacture the entire
>    sweep pattern with an attacker-deployed token for gas money (token movements are taken from
>    provider rows unvalidated), and the pattern cannot distinguish an exchange-controlled intake
>    address from a customer's own habitually-emptied wallet. The claim must be reframed as a
>    *relationship* ("<entity> credited deposits from this address"), not *control*.
> 3. Line citations below drifted by ~2-40 lines in commit `ac4266f`; the substance was re-confirmed.
>
> Also note: `find_sweep_matches` returns a flat list with **no destination grouping** — the concentration
> requirement this feature depends on does not exist yet.

---

## The question that was asked

> For `sweep@1`, does a match against a known hot wallet produce an attribution `Finding`, or does it
> only tag a `Movement` with a pattern label that attribution never consumes?

## The answer, verified directly

**Sweeps are detected, persisted, rendered — and consumed by nothing.** `SWEEP_PATTERN` is written at
`analysis/heuristics/sweep.py:153` and read **nowhere** in `src/`. The only `FindingKind` any production
code reads back is `VASP_ENDPOINT`, at `engine.py:425`, to decide whether to emit a terminal.

There is exactly **one** attributor call site in the entire source tree:

```python
# engine.py:230
results = await self._attributor.attribute(address)
```

It takes only the node's own address, it runs *before* any movement is fetched, and it is never
re-consulted. The detector contract makes this structural rather than accidental:

```python
# engine.py:53-55
Detector = Callable[
    [Address, Sequence["StoredMovement"], Sequence["StoredMovement"]], Sequence[Finding]
]
```

A detector receives no attributor and no labels, so `detect_sweeps` **cannot** know that the address it
forwarded into is a labelled exchange hot wallet.

Attribution itself is a dictionary lookup or nothing (`analysis/attribution/store.py:18-49`). There is
no attributions table, no inferred-attribution concept, and `NodeRow` carries no entity or label column.

## Why this matters more than it looks

Most exchange deposit addresses appear in no labelpack. They are *identified* by behaviour: an address
that receives funds and promptly forwards the full balance into a known exchange hot wallet is, with
high probability, a deposit address belonging to that exchange. This is the standard mechanism by which
a tracing tool gets beyond its direct label coverage.

CipherChain already computes both halves of that inference and then discards the join:

- the **sweep** is detected and filed against the swept address;
- the **hot wallet** is attributed and filed against the hot wallet;
- `EdgeRow` records that the two are connected;
- **nothing joins those three facts.**

Aggravating: when the engine later claims the hot wallet, the label fires and the node terminates at
`engine.py:263-265` *before* `_run_detectors` — so the two sides are split across nodes that never meet.

## Where exactly the chain breaks

- **Last point the information exists:** `sweep.py:99-106`. `SweepMatch` holds the whole `forwarded`
  movement, whose `to_address_id` is the candidate hot wallet.
- **First point it is destroyed:** `sweep.py:151-179`. The `Finding` keeps only the swept address as
  subject and tx hashes as refs. The destination never leaves the function.
- **First place it would need to be consumed:** `engine._run_detectors` — the only scope where a sweep
  result and `self._attributor` coexist. Everything needed is already in hand.

## The shape of the fix (not yet approved)

The precedent exists: `_assess_service` (`engine.py:370-389`) is already "the engine post-processes a
detector's output with knowledge only the engine has." Mirror it with `_assess_deposit_address`:
for each sweep destination, attribute it, and if it is a VASP, emit one finding **about the swept
address**.

Three constraints on any such fix:

1. **It is an inference, never a fact.** Evidence must be the honest triple — `ONCHAIN_FACT` for the
   receive/forward transaction hashes, `HEURISTIC_INFERENCE` carrying `deposit-address@1` with
   confidence `< 1.0`, and `THIRD_PARTY_CLAIM` for the label on the *destination*.
2. **Confidence must sit below the label's own.** An inferred deposit address is strictly weaker
   evidence than a direct hit on the hot wallet.
3. **It must never be written back into the label store.** `AttributionResult` has no field marking
   provenance kind, and `engine.py:517` renders every result as a `THIRD_PARTY_CLAIM` — so inserting an
   inference into the store would launder it into a sourced claim on the next lookup.

## This is planned work, not scope expansion

- `docs/01_PROJECT_VISION.md:43` lists "Confirm a deposit-address heuristic" as a first-class
  investigation objective.
- `:97` names deposit-address detection as the canonical example of heuristic inference.
- `tests/storage/test_repositories.py:179` already uses `heuristic="deposit-address@1"` as a fixture.

**The name is reserved. The code was never written.**
