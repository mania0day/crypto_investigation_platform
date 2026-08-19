# Labelpacks

Drop `*.json` labelpacks in this directory. They are loaded at startup and become
the attribution source that lets CipherChain *name* an endpoint it reaches — the
difference between "the trail ends at address 0x…" and "the trail ends at
Acme Exchange".

CipherChain ships **no invented exchange labels**. A label is a claim about a real
address; a guessed one is a false accusation. The only attribution shipped in the
repo is the vendored OFAC sanctions list, which is published data with a
verifiable source.

## Format

```json
{
  "source": "my-org-vasp-labels",
  "source_date": "2026-08-07",
  "license": "CC0-1.0",
  "default_confidence": 0.8,
  "labels": [
    {
      "chain": "ethereum",
      "address": "0x0000000000000000000000000000000000000000",
      "entity": "Example Exchange (hot wallet)",
      "category": "vasp",
      "confidence": 0.9
    }
  ]
}
```

- `source` is **required** — a label without provenance is not loadable.
- `source_date` should always be set; an undated claim can't be judged for staleness.
- `category`: `vasp` closes a branch and answers the core query; `sanctioned` is
  recorded and the trace continues through it.
- `confidence` must be below 1.0 — a third-party claim is never certainty.
- Addresses are matched case-insensitively for `0x…` chains, exactly otherwise.

## Where labels come from

Legitimate sources include your own investigative records, a commercial feed you
license, or a community tagpack whose licence permits redistribution. Verify the
licence before committing a pack to a public repo.
