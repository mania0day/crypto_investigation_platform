# Provider Capability Matrix

**Status:** research input — the capability-first view that shapes the interfaces in docs 02 (Architecture), 05 (Chain SDK), and 06 (Provider SDK).
**Rule:** the architecture depends on **capabilities, never vendor names**. A chain adapter asks "give me address history"; which vendor answers is the Provider SDK's routing decision, invisible above it. No provider-specific assumption may leak past the SDK boundary.
**Companion:** `PROVIDER_INVENTORY.md` — vendor-centric view, verification status, free-tier notes.

Legend: ✅ key/connectivity verified live (2026-08-07) · ☐ available behind a verified key or keyless, method not yet exercised · ⚠ account exists, dashboard setup required · ✖ gap

> Verification note: ✅ means the provider answered a live call with the stored key. Per-method verification (every capability against every provider) happens when doc 06's record/replay fixtures are built.

---

## 1. Capability catalog

### Chain-scoped (served per chain, consumed by chain adapters)

| Capability | Question it answers | Notes |
| --- | --- | --- |
| `address_history` | Which transactions touched address X? | The tracing workhorse. Raw EVM RPC **cannot** answer this — an indexed source is required. |
| `tx_lookup` | Full transaction by hash | |
| `tx_receipt` | Execution status and logs of a tx | EVM-family. |
| `logs` | Event logs by filter | Input to token/bridge event decoding. |
| `internal_traces` | Contract-to-contract value moves inside a tx | EVM-family; scarce on free tiers. |
| `token_transfers` | Token movements by address or tx | ERC-20 / TRC-20 / SPL — the common case for real flows. |
| `balance` | Current native/token balance | Secondary for tracing; cheap context. |
| `block_lookup` | Block by number/hash | Timestamps for temporal ordering. |
| `utxo_lookup` | Resolve outpoints / address UTXOs | Bitcoin-family only. |

### Chain-agnostic (consumed by normalization and enrichment)

| Capability | Question it answers | Consumer |
| --- | --- | --- |
| `abi_decode` | selector/ABI → human-readable call | Normalization; report readability (`transfer()`, not `0xa9059cbb`) |
| `asset_metadata` | contract → asset identity (symbol, decimals, coin) | Canonical asset registry |
| `protocol_metadata` | contract → protocol/bridge identity | Bridge resolvers, labeling |
| `chain_registry` | chain id → chain configuration | EVM-family-as-configuration |
| `labels` | address → attribution claims | Label store, read by Class F intelligence |
| `name_resolution` | address ↔ human-readable name | Enrichment only — weak third-party claim, never an identification |

Adapters declare which chain-scoped capabilities their chain supports (**capability discovery**, vision principle 4). `internal_traces` on Bitcoin isn't a failure — it's a declared absence.

---

## 2. Bitcoin

| Capability | Primary | Fallback | Status |
| --- | --- | --- | --- |
| `address_history` | mempool.space | Blockstream Esplora | ✅ / ☐ |
| `tx_lookup` | mempool.space | Esplora | ✅ / ☐ |
| `utxo_lookup` | mempool.space | Esplora | ✅ / ☐ |
| `block_lookup` | mempool.space | Esplora | ✅ / ☐ |
| `balance` | mempool.space | Esplora | ✅ / ☐ |
| `tx_receipt` / `logs` / `internal_traces` / `token_transfers` | — declared unsupported by the BTC adapter — | | n/a |

Both are keyless public instances; self-hosting mempool (repo in inventory) is the escape hatch if rate limits bind.

## 3. Ethereum & EVM family

| Capability | Primary | Fallbacks | Status |
| --- | --- | --- | --- |
| `address_history` | Etherscan V2 (`txlist`) | Alchemy `getAssetTransfers`; Blockscout instances | ✅ / ☐ / ☐ |
| `tx_lookup` | RPC pool (dRPC, Ankr, Alchemy) | Infura (after ETH enable), QuickNode / Chainstack (after setup) | ✅ / ⚠ |
| `tx_receipt` | RPC pool | same | ✅ |
| `logs` | RPC pool (`eth_getLogs`) | Etherscan V2 `getLogs` | ✅ / ☐ |
| `internal_traces` | Etherscan V2 (`txlistinternal`) | Tenderly (optional, token needed) | ☐ / ⚠ — **thin, see §7** |
| `token_transfers` | Etherscan V2 (`tokentx`) | Alchemy `getAssetTransfers` | ☐ / ☐ |
| `balance` | RPC pool | Etherscan V2 | ✅ / ☐ |
| `block_lookup` | RPC pool | Etherscan V2 | ✅ / ☐ |

One Etherscan key + `?chainid=` covers the whole Etherscan family; `ethereum-lists/chains` supplies per-chain RPC config. Additional EVM chains are configuration, not code.

## 4. Tron

| Capability | Primary | Fallbacks | Status |
| --- | --- | --- | --- |
| `address_history` | **TronGrid — key not yet obtained** | Tronscan public API | ✖ / ☐ |
| `tx_lookup` | TronGrid | GetBlock (after token setup) | ✖ / ⚠ |
| `token_transfers` (TRC-20) | TronGrid | Tronscan public API | ✖ / ☐ |
| `block_lookup` | TronGrid | GetBlock | ✖ / ⚠ |

**Tron is blocked on one free signup: TronGrid (trongrid.io).** The only missing account for v1 chain coverage.

## 5. Solana

| Capability | Primary | Fallback | Status |
| --- | --- | --- | --- |
| `address_history` | Infura (`getSignaturesForAddress`) | Alchemy | ✅ / ✅ |
| `tx_lookup` (jsonParsed) | Infura | Alchemy | ✅ / ✅ |
| `token_transfers` | derived by the adapter from parsed tx token-balance deltas | — | adapter logic, no extra API |
| `block_lookup` | Infura | Alchemy | ✅ / ✅ |
| `balance` | Infura | Alchemy | ✅ / ✅ |

Solana needs no separate indexer — history is native to RPC. Cost note for budgets: history is a signature list plus one `tx_lookup` **per signature** (fan-out), so Solana traces consume budget faster than EVM ones.

## 6. Chain-agnostic

| Capability | Sources | Status |
| --- | --- | --- |
| `abi_decode` | 4byte.directory, OpenChain, Sourcify, Etherscan `getabi` | ☐ all free — enrichment, not urgent |
| `asset_metadata` | CoinGecko public API; on-chain ERC-20 calls via RPC pool | ✅ / ✅ |
| `protocol_metadata` | DefiLlama (`api.llama.fi`) | ✅ |
| `chain_registry` | `ethereum-lists/chains` (static repo, vendored) | ☐ |
| `labels` | OFAC repo (license-clean); EntityAddressBitcoin (**license check pending**); open tagpacks (future, doc 07) | ☐ |
| `name_resolution` | ENS (via RPC), Unstoppable Domains, Solana Name Service (via Solana RPC) | ☐ — pure enrichment |

---

## 7. Gaps the matrix exposes

1. **Tron `address_history`** — ✖ until the TronGrid key exists (user action; free).
2. **EVM `internal_traces`** — one real source (Etherscan `txlistinternal`); `debug_`/`trace_` RPC methods are generally paid-tier. Design consequence: the engine must treat internal traces as an **optional capability** and degrade explicitly when absent (vision principle 9), because ETH-value flows through contracts are invisible without them.
3. **Labels** — seeds only. VASP deposit/hot-wallet coverage for EVM/Tron/Solana is doc 07's central problem.
4. **Solana history fan-out** — not a provider gap but a budget pressure; the planner must weigh Solana expansions accordingly.

---

## 8. Provider middleware pipeline (design input for doc 06)

Every provider call, no exceptions:

```
Capability Router → Cache → Rate Limiter → Retry Policy → Circuit Breaker → vendor call
        └──────────────────── Metrics wrap the entire pipeline ────────────────────┘
```

- **Capability Router** — (chain, capability) → ranked provider list. Ranking is configuration + live metrics, not code.
- **Cache** — checked **before** the rate limiter: chain data is immutable, and a cache hit must cost zero rate budget. Content-addressed, shared across investigations, never expires.
- **Rate Limiter** — per-key token buckets pinned at/below each free tier.
- **Retry Policy** — idempotent reads retry with backoff + jitter; 429s feed back into the limiter.
- **Circuit Breaker** — a failing provider leaves rotation; periodic probes re-admit it. Router falls through to the next provider.
- **Metrics** — per provider × capability: success %, latency (p50/p95), cache-hit %, 429 count, failure count, fallback count. These numbers drive the router's ranking and tell us when a free tier stops being enough.

## 9. Budget taxonomy (design input for doc 03)

Two enforcement layers, both required:

| Layer | Budgets | Enforced by | Protects |
| --- | --- | --- | --- |
| Investigation | API-call, time, depth, node budgets | Planner (doc 03) | The investigation from supernodes and runaway expansion |
| Provider | Per-key rate limits | Provider SDK (doc 06) | The quotas from the investigation |

## 10. The boundary this matrix respects

Class F intelligence — bridge detection, VASP detection, mixer detection, cluster detection, sanctions screening, (later) ML — appears **nowhere** in this matrix, by design. Intelligence consumes normalized investigation data and never calls providers (vision principle 1). Capabilities end at normalization; intelligence begins after it. The OFAC dataset (Class D) is *data*; sanctions *screening* is Class F logic consuming it — the same split applies to every detector.
