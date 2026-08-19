# CipherChain — Project Vision

**Document:** 01 of 11 · **Status:** ✅ APPROVED & FROZEN (2026-08-07) — changes only on discovery of a real architectural flaw · **Last updated:** 2026-08-07

> The engine is the product. The graph is only the visualization of the investigation.

---

## 1. Mission

CipherChain is an open-source autonomous blockchain investigation platform. Given a blockchain address, it investigates the flow of funds in both directions — backward through funding history, forward through cash-out — across chains, and answers one question with evidence:

> **Where did these funds come from, and where did they go — specifically, what is the nearest previous VASP and the nearest next VASP?**

### The core query

Every part of the system exists to answer this query, so it is defined precisely.

Given an address — optionally scoped to an asset and a time window — CipherChain determines, for each traced flow of value:

- **Nearest previous VASP** — the first attributed service endpoint reached when tracing the flow backward through its funding history.
- **Nearest next VASP** — the first attributed service endpoint reached when tracing the flow forward toward cash-out.

#### Refinement (2026-08-11): each direction may have TWO answers

"Attributed" covers two different things, and collapsing them was hiding information from the
investigator. An endpoint may be attributed by a **sourced label** that names its operator, or by a
**behavioural inference** that establishes it acts as custodial infrastructure without naming anyone.
Both are legitimate answers; they answer different questions:

- **Nearest endpoint** — the closest attributed endpoint, whatever its basis. *What is closest?*
- **Nearest named endpoint** — the closest endpoint carrying a `third_party_claim`. *What can I act
  on?* An investigator can subpoena Binance. Nobody can subpoena "custodial infrastructure, operator
  unnamed, 61%".

**Both are reported, always, labelled as distinct answers.** When the nearest endpoint is itself
named they are one answer and it is stated once. CipherChain does not rank them against each other:
preferring confidence would bury a nearer endpoint, preferring proximity would bury a named one, and
either choice would be an invisible judgement made on the investigator's behalf — which principle 6
forbids. Selection lives in `investigation/answers.py` so that every consumer stating "nearest
previous/next VASP" states the same thing.

This became load-bearing once labels were read at discovery
(`docs/research/ATTRIBUTION_AT_DISCOVERY.md`): before that the two rarely coexisted, and the
presentation layer picked whichever finding was recorded first — traversal order, which is not a
rule and quietly stopped being right the moment a hop-2 label was filed before a hop-1 inference.

Each answer carries: the attributed entity, the direction, the share of traced value it accounts for, the transaction path that supports it, and a confidence derived from the evidence. Tracing is time-respecting: value cannot leave an address before it arrived.

When no attributed endpoint can be reached, that is also an answer — an explicit terminal finding ("funds entered an unsupported bridge," "trace reached its budget," "flow ends at an unattributed address") stating exactly where the trace stopped and why.

### The success definition

CipherChain succeeds when it answers the investigation query correctly, with evidence for every conclusion. It does not succeed by rendering a graph. The graph exists only to explain the answer.

---

## 2. Investigation Philosophy

CipherChain is a **goal-directed investigation engine**.

The engine never expands the graph blindly. Every expansion must pursue an explicit investigation objective, such as:

- Find the previous VASP.
- Find the next VASP.
- Traverse a bridge to its destination chain.
- Confirm a deposit-address heuristic.
- Resolve an unknown entity.

When objectives are achieved, or when investigation limits are reached, expansion stops. An expansion that serves no objective does not happen — regardless of how cheap it is or how interesting the neighborhood looks.

The consequences of this philosophy:

- **The investigator does not manually expand nodes.** They state what they want to know; the engine decides what to fetch to find out.
- **Every fetched transaction is attributable to a reason.** The investigation record can explain why each piece of data was retrieved.
- **The graph is a byproduct.** It is the visualization of the investigation the engine performed — not the product, and not the workspace.

---

## 3. What CipherChain Is Not

CipherChain is defined by its mission, not by resemblance to neighboring tools.

- **Not a block explorer.** It does not exist to browse chain data.
- **Not a portfolio tracker.** It has no notion of holdings, prices, or profit.
- **Not a graph visualization tool.** Visualization is an output, not the purpose.
- **Not a chain indexer.** CipherChain materializes only the data that investigations touch. It never ingests entire chains, and it must remain deployable without indexing infrastructure.

### Non-goals for v1

Declared explicitly so scope cannot creep silently.

| Non-goal | Rationale |
| --- | --- |
| **Mixer demixing** | CipherChain detects and evidences mixer interaction; it does not attempt de-anonymization. Demixing is unreliable, research-grade, and legally sensitive. |
| **Real-time monitoring / alerting** | Watching live flows is a different product shape (streaming, notification infrastructure). V1 is retrospective investigation. |
| **Risk-scoring API** | Numeric risk scores invite overclaiming beyond what the attribution data supports. CipherChain produces findings with evidence, not scores. |
| **ML-driven prioritization** | Expansion policy ships as explainable heuristics. A learned policy may later sit behind the same interface. |

A non-goal is not a value judgment; several may become goals in later versions. In v1 they are out.

---

## 4. Evidence Posture

CipherChain is an **evidence-first forensic architecture designed for investigative workflows**.

### What v1 commits to

1. **Reproducible investigations** — the same inputs, processed by the same versions, produce the same findings.
2. **Evidence provenance** — every finding cites the data it rests on, and every piece of data records where and when it was obtained.
3. **Versioned heuristics** — every inference names the heuristic and version that produced it.
4. **Versioned label datasets** — every attribution names its dataset, source, and date.
5. **Immutable investigation records** — once recorded, investigation data and findings are never silently rewritten.

### The evidence taxonomy

A finding may rest on four kinds of support, and they are never conflated:

- **On-chain fact** — data any third party can independently verify against the blockchain.
- **Heuristic inference** — a conclusion produced by a named, versioned heuristic, always carrying a confidence. Example: co-spend clustering, deposit-address detection.
- **Third-party claim** — an attribution asserted by an external source, always carrying that source and its date. Example: a label identifying an address as an exchange.
- **Engine observation** — a statement about CipherChain's own run: what it examined, where it stopped, and what it never looked at. Verifiable against the investigation record rather than against the chain, and never carrying a confidence, because the engine does not guess about its own behaviour. Example: "3 addresses had more history than one page — their older transactions were never read."

> **Amendment (2026-08-09).** The taxonomy originally had three kinds. Terminal
> findings — "the trail ends here" — had no honest kind to sit in and were filed
> as on-chain facts with an address as their reference, which claimed chain
> verifiability for a statement about the tool. The fourth kind **narrows** what
> *on-chain fact* means rather than widening the taxonomy's reach: that stamp is
> now reserved strictly for what anyone can check against the ledger. Recorded
> under Ruling 4 in `docs/research/NEXT_MILESTONE_DECISIONS.md`.

### What is not claimed

CipherChain supports evidence-backed investigation; it does not promise legal acceptance. It makes no claim of court admissibility or legal certification. Full chain-of-custody tooling is out of scope for v1. CipherChain is decision support: the investigator draws conclusions; CipherChain supplies the evidence.

---

## 5. Investigation Workflow

One investigation, end to end:

1. **Intake.** The investigator submits an address, optionally scoped by asset and time window, and selects objectives (by default: nearest previous VASP and nearest next VASP).
2. **Planning.** The engine translates objectives into an expansion plan with explicit limits — depth, breadth, value-share thresholds, and a total budget.
3. **The investigation loop.** Repeatedly: expand where an objective demands it → normalize what was fetched → run attribution and detection on the normalized data → re-plan. Results feed back into planning: reaching an attributed endpoint closes that branch; detecting a mixer flags and terminates it; a supported bridge crossing continues the trace on the destination chain.
4. **Findings.** Conclusions accumulate as evidence-backed findings while the investigation runs — attributions reached, patterns detected, terminals encountered.
5. **Report.** The investigation produces its answer to the core query, with every finding traceable to its evidence. The report is the deliverable; the graph explains it.

Two properties of every investigation:

- **Steerable autonomy.** Autonomous by default; interruptible always. The investigator can pause, pin, exclude, and force-expand mid-run without restarting the investigation.
- **Explicit gaps.** When a data source fails or a limit is reached, the result is partial and says so, stating exactly what was not examined. A silent gap in an evidence product is disinformation; CipherChain never presents an incomplete trace as complete.

Investigations are long-running background work: they survive restarts, resume from where they stopped, and can be extended with a larger budget without repeating completed work.

---

## 6. Scope

### Chains (v1)

V1 targets four flagship chains spanning three execution paradigms, because the chain-agnostic claim is only credible if the abstraction survives genuinely different models:

| Chain | Paradigm | Order |
| --- | --- | --- |
| Bitcoin | UTXO | 1 — walking skeleton |
| Ethereum | EVM account | 1 — walking skeleton |
| Tron | EVM-family account | 2 |
| Solana | Solana runtime | 3 |

Bitcoin and Ethereum come first *together*: the two most different models force the canonical data model and the adapter interface to be real before anything else is built on them.

**Broad coverage is achieved architecturally, not by enumeration.** The EVM-family adapter is parameterized so that additional EVM chains (Polygon, BSC, Arbitrum, Base, …) are configuration, not new code. Chains with new paradigms arrive through the Chain SDK as independent adapters.

### Assets

Asset-awareness from day one. Investigations follow native coins and fungible tokens, and stablecoin flows are treated as the common case, not an extension. The engine follows **value**: a flow that passes through a swap continues as the destination asset.

### Capabilities (v1)

- Backward tracing (funding history) and forward tracing (cash-out flow).
- VASP and mixer identification through attribution data and deposit heuristics.
- Bridge traversal for supported bridges; unsupported or unresolved crossings become explicit terminal findings.
- Detection of common laundering patterns (peel chains, rapid pass-through, and similar), each as a named, versioned heuristic.
- Evidence-backed findings and an exportable investigation report.

### Critical dependency: attribution data

Tracing without attribution answers nothing: the engine can only report "nearest VASP" if it recognizes a VASP when it reaches one. Attribution is therefore a first-class subsystem, not an afterthought:

- Attribution sources are **pluggable**, and label datasets interoperate with open community tag formats.
- Seed datasets are built exclusively from **license-clean sources**.
- Every label is a **claim, not ground truth**: sourced, dated, and confidence-scored, per the evidence taxonomy.

---

## 7. Core Principles

Every design and code review checks against these. Each is phrased so that a violation is detectable.

### Boundaries

1. **Only Chain Adapters communicate with blockchain providers.** No detector, heuristic, ML model, graph component, or investigation component may call a blockchain API. All intelligence operates exclusively on normalized investigation data. *(Non-negotiable.)*
2. **Every module has one responsibility.** A module that needs "and" to describe itself is two modules.
3. **Chain adapters are completely independent** of one another. No adapter imports another; shared behavior lives in the Chain SDK.
4. **Every chain implements the same adapter interface** and declares its capabilities explicitly, so the engine adapts to what a chain supports instead of assuming a lowest common denominator.
5. **The investigation engine is chain-agnostic.** It operates on the canonical data model only and never branches on a chain's identity.
6. **No God classes.** Orchestration is decomposed — planning, expansion, normalization, detection, reporting — and no single class owns the investigation.

### Evidence

7. **Every finding contains evidence**, classified by the evidence taxonomy — fact, inference, or claim — with inference and claim always carrying version, source, and confidence.
8. **Investigations are reproducible.** The same inputs plus the same versions produce the same findings.
9. **Results degrade gracefully.** Failures and exhausted limits yield partial results with explicit gaps, never silent holes.

### Autonomy

10. **Expansion is objective-driven.** Every fetch pursues a stated investigation objective; expansion stops when objectives are achieved or limits are reached.
11. **Autonomy is steerable.** The investigator can redirect a running investigation without restarting it.
12. **Long investigations run as resumable background jobs** — checkpointed, restart-safe, and extendable.

### Sustainability

13. **CipherChain is not an indexer.** Only data that investigations touch is materialized. Fetched chain data is immutable and cached permanently.
14. **Extensibility through defined extension points** — chain adapters, detectors, cross-chain resolvers, attribution sources — not through speculative abstraction.
15. **A feature is done when evidence shows it works.** Detection logic is validated against recorded fixtures and known public cases that run as regression tests.

---

## 8. Development Method

Design documents are finalized one at a time, in order, before implementation of their scope begins:

```
docs/
01_PROJECT_VISION.md      ← this document
02_ARCHITECTURE.md
03_INVESTIGATION_ENGINE.md
04_DATABASE.md
05_CHAIN_SDK.md
06_PROVIDER_SDK.md
07_INTELLIGENCE_ENGINE.md
08_API_SPEC.md
09_FRONTEND.md
10_ROADMAP.md
11_CLAUDE.md
```

**Reality checkpoint.** After document 03 is finalized, a minimal walking skeleton — one known Bitcoin case and one known Ethereum case, traced end to end, headless — validates the canonical model against real chain data before documents 04–07 are finalized. Design stays ahead of code; code keeps the design honest.

**The feature gate.** No feature is built unless all four questions have answers:

1. Why does this feature exist?
2. Where does it belong in the architecture?
3. Does it violate any architectural principle?
4. What evidence would prove it works?

---

*Changes to this document require explicit approval. Later documents inherit its constraints; where they conflict, this document wins.*
