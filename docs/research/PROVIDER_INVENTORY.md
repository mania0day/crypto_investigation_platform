# Provider & Data Resource Inventory

**Status:** research input — feeds docs 02 (Architecture), 06 (Provider SDK), 07 (Intelligence Engine). Not a numbered design doc and not gated.
**Last verified:** 2026-08-07, with one live call per resource.
**Rule:** this file is tracked — it must never contain credentials. Keys live in `.env` (gitignored).
**Companion:** `CAPABILITY_MATRIX.md` — the capability-first view the architecture depends on. Docs 02/05/06 derive interfaces from the matrix, not from this vendor list.

---

## Provider classes

The Provider SDK (doc 06) will formalize these roles. The class matters more than the vendor: the engine asks for a *capability*, the SDK routes to whichever configured provider serves it.

| Class | Role | Why it exists |
| --- | --- | --- |
| **A — Indexed history** | Answers "all transactions/transfers for address X" | Required for tracing. Raw EVM RPC cannot answer this question at all. |
| **B — Raw JSON-RPC** | Fetch tx / receipt / logs / block by hash | Verification, event decoding, bridge resolution. Pooled, with rotation and failover. |
| **C — Enrichment / metadata** | Assets, protocols, chain registries, names | Normalization and context; never load-bearing for conclusions. |
| **D — Attribution seeds** | Label datasets | Third-party claims per the evidence taxonomy (vision §4): sourced, dated, confidence-scored. |
| **E — Dev tooling** | Not in the investigation data plane | Development and documentation support only. |
| **F — Intelligence** | *Consumers, not sources:* bridge detection, VASP detection, mixer detection, cluster detection, sanctions screening, (later) ML | Operates exclusively on normalized investigation data and never calls providers (vision principle 1). Listed so the boundary is explicit: Classes A–E feed normalization; Class F consumes what normalization produced. |

---

## Verified inventory

| Resource | Class | Chains | Status 2026-08-07 | Notes |
| --- | --- | --- | --- | --- |
| Etherscan V2 API | A | 50+ EVM chains, one key, `?chainid=` | ✅ verified | Address tx lists, token transfers, internal txs. The workhorse for EVM tracing. Free tier ~5 req/s — treat as scarce. |
| mempool.space API | A | Bitcoin | ✅ keyless | Public instance, be polite; self-hostable (repo below) if quota hurts. Add Blockstream Esplora as failover. |
| Solana RPC (native) | A + B | Solana | ✅ via Infura & Alchemy | `getSignaturesForAddress` means Solana needs no separate history indexer — any healthy RPC is also Class A. |
| Alchemy | A + B | ETH, L2s, Solana | ✅ ETH + Solana verified | `alchemy_getAssetTransfers` is a second EVM history source (failover for Etherscan). URL per network: `{network}.g.alchemy.com/v2/{key}`. |
| dRPC | B | ETH + many | ✅ verified (both URL formats) | Load-balanced multi-provider RPC. |
| Ankr | B | ETH + many | ✅ RPC verified | Advanced API (`ankr_getTransactionsByAddress`) is **disabled on the current tier** — RPC only for now. |
| Infura | B | Solana ✅ / ETH ❌ | partial | ETH mainnet not enabled on this project — one dashboard toggle away if needed. Solana works. |
| QuickNode | B | many | ⚠ needs setup | Bare key rejected (403). An endpoint must be created in the dashboard; its full URL is the credential. |
| Chainstack | B | many | ⚠ needs setup | Platform key valid, but it manages the account; deploy a node to obtain an RPC endpoint. |
| GetBlock | B | many, incl. **Tron** and BTC | ⚠ needs setup | Node provider with a free shared tier. Main value to CipherChain: Tron RPC. Generate an access token first. |
| CoinGecko public API | C | all | ✅ keyless | Token contract → asset mapping across chains; input to the canonical asset registry. |
| DefiLlama API (`api.llama.fi`) | C | all | ✅ keyless | Protocol, chain, and bridge metadata — useful for labeling DeFi and bridge contracts. Free tier needs no key. |
| ENS | C | Ethereum | docs reviewed | Reverse resolution (address → name) as enrichment. Evidence-wise a weak third-party claim, never an identification. |
| ABI decoding — 4byte.directory, OpenChain, Sourcify (+ Etherscan `getabi`) | C | EVM family | keyless / free | Selector → signature and verified-contract ABIs. Without this, reports show `0xa9059cbb` instead of `transfer()`. Evidence-readability enrichment; not urgent, documented for doc 06. |
| Name resolution — Unstoppable Domains, Solana Name Service | C | multi / Solana | reference | With ENS: address ↔ human-readable names. Pure enrichment — weak third-party claims, never identification. |
| `ethereum-lists/chains` (repo) | C | EVM family | reference | Canonical chain registry (chain IDs, RPC URLs, explorers). **This is the config source that makes "EVM chains as configuration" real.** |
| OFAC sanctioned addresses (`0xB10C/ofac-sanctioned-digital-currency-addresses`) | D | multi | reference | Machine-readable, auto-updated SDN addresses. License-clean attribution seed — exactly the kind the vision requires. |
| `Maru92/EntityAddressBitcoin` (repo) | D | Bitcoin | reference | Research-era BTC entity labels. **Verify license and staleness before ingestion**; treat as low-confidence claims with source + date. |
| Blockscout (repo + public instances) | A fallback | EVM | reference | Explorer API for EVM chains outside the Etherscan family; self-host option later. |
| mempool (repo) | A self-host | Bitcoin | reference | Self-hosted BTC explorer/API if the public instance's limits bind. |
| Tenderly | E | EVM | ⚠ token needed | Transaction simulation and decoded call traces. Optional enrichment for evidence readability; not core, adopt only if a concrete need appears. |
| Scalar | E | — | account exists | API-documentation hosting — candidate for publishing CipherChain's OpenAPI reference (doc 08). Not a blockchain provider. |

---

## Coverage against the v1 chain plan

| Chain | History (Class A) | Raw RPC (Class B) | Verdict |
| --- | --- | --- | --- |
| Bitcoin | mempool.space (keyless) | mempool.space / Esplora | ✅ enough for the walking skeleton |
| Ethereum + EVM family | Etherscan V2, Alchemy | dRPC, Ankr, Alchemy (+ QuickNode/Chainstack after setup) | ✅ strong — deep failover pool |
| Tron | **— GAP —** | GetBlock (after token setup) | ⚠ **needs a TronGrid key** (free, trongrid.io) — the only signup CipherChain still lacks |
| Solana | native via RPC | Infura, Alchemy | ✅ covered |

## Attribution gap (the existential dependency)

OFAC sanctions and the BTC entity dataset are **seeds**, not coverage. VASP deposit/hot-wallet labels for EVM, Tron, and Solana remain the thin spot. This is doc 07's central problem and stays flagged until it has a design.

---

## Wise-use rules these free tiers impose (→ docs 02 / 06)

1. **Cache-first.** Chain data is immutable: fetch once, store forever, never refetch. A quota spent twice on the same tx is a bug.
2. **Budgets.** Every investigation carries API-call, time, depth, and node budgets, enforced by the planner (doc 03) — distinct from per-provider rate limits, enforced by the Provider SDK (doc 06). Expansion is objective-driven (vision §2), so every call is attributable to a reason.
3. **Class routing + failover.** History queries go to Class A; verification to the Class B pool with rotation. Provider outage degrades the investigation gracefully (vision principle 9), never silently.
4. **Keys are deployment config, never architecture.** The engine depends on provider *interfaces*; vendors are plugins configured in `.env`. Any key can be revoked without touching code.
5. **Instrumented pool.** Every provider call passes through the middleware pipeline — capability router, cache (checked first: a cache hit costs zero rate budget), rate limiter, retry, circuit breaker — with per-provider × per-capability metrics (success %, latency, cache-hit %, 429s, failures, fallbacks). Details: `CAPABILITY_MATRIX.md` §8.
