# Bridge packs

Drop `*.json` bridge packs here. They tell CipherChain which contracts are bridges,
which turns a dead end into a direction:

> *"value entered bridge 'polygon-pos' toward polygon — the trail continues off
> this chain"*

instead of *"trail ends here"*.

**CipherChain ships no bridge addresses.** A bridge address asserted from memory is
the same failure as an invented exchange label: the tool would tell an
investigator that funds crossed a bridge on the strength of a guess. Every entry
needs a source you can point at — the protocol's own documentation or repository.

## Format

```json
{
  "source": "polygon-official-docs",
  "source_date": "2026-08-07",
  "bridges": [
    {
      "bridge_id": "polygon-pos",
      "name": "Polygon PoS Bridge (ERC20 predicate)",
      "chain": "ethereum",
      "address": "0x0000000000000000000000000000000000000000",
      "direction": "deposit",
      "counterpart_chain": "polygon"
    }
  ]
}
```

- `source` is **required** — no provenance, no load.
- `chain` is where the contract lives; `counterpart_chain` is where funds go
  (omit if the bridge is multi-destination or unknown).
- `direction`: `deposit` (value entering the bridge on this chain) or
  `withdrawal` (value arriving from another chain).
- Hex addresses match case-insensitively.

## Getting addresses right

Two practical traps:

- **Proxy vs implementation.** Most bridges are upgradeable proxies. Register the
  address users actually send to (the proxy), not the implementation behind it.
- **Deprecated bridges.** Several major bridges have been exploited and retired.
  Historical traces still need them, so keep them and note the status in `name`.

## What this does and does not do

A pack lets an adapter emit a **bridge hint** for its own chain — "value entered
this bridge here". Matching that deposit to its payout on the destination chain
is a separate problem requiring data from both chains, and is not yet
implemented. Today the finding tells an investigator *where to look next*, which
is the majority of the value.
