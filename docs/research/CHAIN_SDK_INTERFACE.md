# Chain SDK — Interface Freeze RFC

**Status:** ✅ FROZEN (approved 2026-08-07). Every other bounded context depends on this contract; changes require the same approval as a vision change.
**Rulings:** D1 — BridgeHints from `normalize()`, cross-chain resolution in `analysis/bridges`. D2 — `normalize()` is async and may fetch supporting data through the pool. D3 — `address_history` returns full `ChainTransaction`s. D4 — the five written provider primitives are kept; §3 contracts approved as part of this freeze.

---

## 1. The frozen surface

```python
class ChainAdapter(ABC):
    """One blockchain's semantics behind a uniform contract.

    Invariants (frozen vision doc 01):
    - Adapters are the ONLY components that reach providers, and only via
      the ProviderPool (principle 1).
    - Adapters are completely independent of each other (principle 3);
      shared behavior lives in the Chain SDK base, never in another adapter.
    - Everything returned upward is canonical-model data; raw payloads never
      cross the SDK boundary except opaquely inside ChainTransaction.
    """

    chain: str  # canonical chain id: "bitcoin", "ethereum", "tron", "solana"

    def capabilities(self) -> frozenset[Capability]:
        """Declared capabilities (principle 4). Absence is an answer, not a failure."""

    def supports(self, capability: Capability) -> bool:
        """Convenience over capabilities()."""

    async def address_history(
        self,
        address: Address,
        *,
        window: TimeWindow | None = None,
        cursor: str | None = None,
        limit: int = 100,
    ) -> HistoryPage:
        """Transactions touching the address, newest first, paginated.
        Raises CapabilityNotSupported if ADDRESS_HISTORY is not declared."""

    async def transaction(self, tx_hash: str) -> ChainTransaction:
        """Fetch one transaction. Raises ResourceNotFound if it does not exist."""

    async def normalize(self, tx: ChainTransaction) -> NormalizedTransaction:
        """Chain knowledge → canonical facts (+ bridge hints). See D2 for
        why this is async."""
```

## 2. Supporting types (chain-agnostic, live in the SDK)

```python
@dataclass(frozen=True, slots=True)
class TimeWindow:
    start: datetime | None = None
    end: datetime | None = None

@dataclass(frozen=True, slots=True)
class ChainTransaction:
    """A transaction as acquired, with provenance. `raw` is opaque above the
    SDK — only the owning adapter's normalize() understands it."""
    chain: str
    tx_hash: str
    raw: object
    provenance: Provenance

@dataclass(frozen=True, slots=True)
class HistoryPage:
    items: tuple[ChainTransaction, ...]
    next_cursor: str | None          # opaque chain-specific token; None = exhausted

class BridgeDirection(StrEnum):
    DEPOSIT = "deposit"              # value entering the bridge on this chain
    WITHDRAWAL = "withdrawal"        # value leaving the bridge on this chain

@dataclass(frozen=True, slots=True)
class BridgeHint:
    """Chain-local evidence that a tx touched a known bridge. Produced by
    normalize() from chain-specific knowledge (contract addresses, event
    signatures). Cross-chain MATCHING of hints into edges happens in
    analysis/bridges — never inside an adapter (see D1)."""
    bridge_id: str                   # from the bridge registry
    direction: BridgeDirection
    counterpart_chain: str | None    # declared destination when decodable
    tx: TxRef
    refs: tuple[str, ...]            # evidence refs (event ids, payload digests)

@dataclass(frozen=True, slots=True)
class NormalizedTransaction:
    tx: TxRef
    movements: tuple[Movement, ...]
    bridge_hints: tuple[BridgeHint, ...] = ()

class ChainRegistry:
    def register(self, adapter: ChainAdapter) -> None: ...
    def get(self, chain: str) -> ChainAdapter: ...      # unknown chain → error
    def chains(self) -> tuple[str, ...]: ...
```

**EVM family as configuration:** one `EvmAdapter` class, N instances built from `EvmChainConfig` (chain id, Etherscan chainid, RPC endpoints). Registering Polygon is a config entry, not a new adapter.

## 3. Provider-plane contracts the adapters consume (review with this RFC)

Already written, held open for revision until this freeze:

```python
@dataclass(frozen=True)
class ProviderRequest:
    chain: str
    capability: Capability
    params: Mapping[str, Any]        # NEVER contains credentials
    def cache_key(self) -> str       # sha256 of (chain, capability, params)

@dataclass(frozen=True, slots=True)
class ProviderResponse:
    provider: str
    retrieved_at: datetime
    payload: Any                     # parsed, capability-specific value
    raw: bytes                       # exact vendor bytes — the evidence digest source
    payload_sha256: str
    from_cache: bool
    def provenance(self) -> Provenance

class Provider(ABC):                 # implemented by vendor clients only
    def supports(self, chain: str, capability: Capability) -> bool
    async def execute(self, request: ProviderRequest) -> ProviderResponse
```

Adapters call `pool.fetch(ProviderRequest(...))` and never a vendor client directly. Vendor names surface only in provenance and metrics.

## 4. Decision points — need a ruling before the freeze

**D1 — Where does `resolve_bridge` live?**
Your sketch places `resolve_bridge()` on ChainAdapter. Recommendation: adapters emit `BridgeHint`s from `normalize()` (chain-local knowledge: contracts, event signatures) and **cross-chain resolution lives in `analysis/bridges`**, consuming stored normalized data from both chains. Rationale: matching a deposit to its destination-chain payout requires knowledge of *two* chains — putting it on an adapter violates adapter independence (principle 3) or forces adapters to call across chains. The frozen vision (§principle 1 + Class F boundary) already decided this shape.
Alternative: literal `resolve_bridge()` on the adapter — would need access to other adapters; recommend against.

**D2 — Is `normalize()` async and allowed to fetch through the pool?**
Recommendation: yes. Normalization sometimes needs supporting lookups (token decimals, receipts for token events, prevout resolution) — all cached, all provenance-tracked. This is still acquisition-side work; keeping it inside the adapter is exactly why nothing above the SDK ever needs a chain-specific fetch.
Alternative: pure sync `normalize()` with a separate pre-fetch step — more plumbing, no boundary gain.

**D3 — What does `address_history` return?**
Recommendation: full `ChainTransaction`s (the adapter absorbs per-chain fan-out, e.g. Solana's signature-list → per-tx fetch; the pool cache dedupes; true API cost is visible in pool metrics).
Alternative: return bare tx refs and let the engine fetch each — finer-grained budget control, much chattier engine.

**D4 — The five provider files already written** (token bucket, circuit breaker, metrics, cache, base contracts).
Recommendation: keep — the primitives are contract-neutral, and the contract-sensitive part (§3) is being reviewed right here. The pool orchestrator and vendor clients stay unwritten until after this freeze.
Alternative: delete all five and rewrite after the freeze.

## 5. What each v1 adapter does with this contract

- **Bitcoin:** `address_history`/`transaction` via mempool.space (prevouts included in payloads); `normalize()` emits UTXO input/output halves as Movements; capabilities exclude receipts/logs/token transfers (declared absence).
- **EVM (Ethereum first):** history via Etherscan V2 `txlist` + `tokentx`; addresses lowercased, amounts as int wei; `normalize()` emits native + token Movements and BridgeHints from the bridge registry; internal traces declared optional.
- **Tron / Solana:** same contract, later — Tron blocked on the TronGrid key; Solana via Infura/Alchemy RPC.
