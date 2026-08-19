# Verified assets

Token contracts whose provenance CipherChain has established. **A heuristic may only point at movements
denominated in a native asset or a contract listed here.**

## Why this exists

A token contract can emit `Transfer` events between addresses that never signed anything. Without a
floor, a third party can deploy a worthless token and — for the price of gas on a dozen cheap
transactions — manufacture a complete receive-and-forward pattern for a victim of their choosing,
pointed at any destination they like. The victim does nothing and consents to nothing.

Nothing downstream can undo that. The movements are real *events*, faithfully normalized. The only
defence is upstream: refuse to found an inference on an asset nobody verified.

Measured on a real Ethereum wallet: trusting every asset produced 23 findings; trusting only verified
assets produced 18. The five that disappeared rested on unverified token movements — including one
service-endpoint inference whose counterparty degree was partly forged-token noise.

## What this is *not*

It is **not** a filter on traversal. Movements in unverified assets are still stored and still expand
the graph, because they are real events and hiding them would be its own dishonesty. They simply
cannot be the evidence a finding points at.

It is also **not** a claim that a listed token is good, safe, or valuable — only that CipherChain knows
which contract it is.

## Format

```json
{
  "source": "where these came from",
  "source_date": "YYYY-MM-DD",
  "license": "CC0-1.0",
  "assets": [
    {
      "chain": "ethereum",
      "contract": "0x...",
      "symbol": "USDT",
      "decimals": 6,
      "issuer": "Tether",
      "onchain_symbol": "USDT",
      "source_url": "https://..."
    }
  ]
}
```

`source` and `source_date` are mandatory — a pack lacking either is refused at load, not warned about.

EVM contracts are matched case-insensitively; Tron and Solana addresses are Base58 and
case-**significant**, so they are matched verbatim.

## Adding an asset

Same standard as `labels/` and `bridges/` — **ship only what you have confirmed**:

1. Read the contract address from the **issuer's own documentation**, not from an explorer's search
   box and not from a community list.
2. Confirm it on-chain before adding it:
   - **EVM** — `eth_getCode` must be non-empty, and `symbol()` / `decimals()` must match.
   - **Tron** — `wallet/getcontract` must return bytecode; confirm `decimals()`.
   - **Solana** — `getAccountInfo` must report an owner of `TokenkegQfeZyiNwAJbNbGKPFXCWuBvf9Ss623VQ5DA`.
3. Record the on-chain symbol verbatim when it differs from the common name (Polygon USDT currently
   reports `USDT0` after Tether's omnichain migration — recorded as found, not normalised).

An empty or absent directory is safe, not broken: heuristics fall back to native assets only, which
is the one asset class that cannot be forged.
