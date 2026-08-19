# CipherChain API reference

The interactive, always-current version of this document is served by the
running instance at **`/docs`** (OpenAPI/Swagger, generated from the code).
This page exists for reading away from a running server; where the two ever
disagree, `/docs` is right.

**Base URL**: wherever the server runs (`backend/scripts/demo.sh` binds
`127.0.0.1:8000`). **Auth**: none yet — the API relies on its local bind. The
intel endpoints (below) ship behind an API-key layer per
`research/LABEL_INTELLIGENCE.md` §6.

---

## Investigations

### `POST /investigations` — start a trace

```json
{
  "address": "0x6bd0b42faf093541b31f94a041774d5eb30906ad",
  "objectives": ["find_prev_vasp", "find_next_vasp"],
  "budgets": { "api_calls": 25, "seconds": 300.0, "max_depth": 3, "max_nodes": 500 }
}
```

- `objectives` — at least one of `find_prev_vasp` (where funds came from)
  and `find_next_vasp` (where they went).
- `chain` — optional; omitted, the chain is detected from the address format.
  Supply it only to disambiguate formats several chains share (EVM chains).
- `budgets` — every field optional (defaults shown above are the schema's,
  not the demo UI's). Two more fields govern what happens when a budget runs
  out: `pursue_until_answered` (default `true`) lets the run grant itself
  another allowance of the same size while an objective still has no **named**
  endpoint and addresses are still queued, and `max_extensions` (default `8`)
  is the ceiling on that. Only the cost budgets are ever extended —
  `max_depth` decides what the trace means and is never raised — and every
  grant is recorded, reaching the report and `coverage.budget_extensions`.

Returns `201` with `{ investigation_id, status, chain }` — `chain` echoes
what was detected. The investigation runs in the background; poll status.
`422` if the chain cannot be resolved.

### `GET /investigations/{id}` — status

```json
{
  "investigation_id": "…", "status": "running", "chain": "ethereum",
  "root_address": "0x…", "objectives": ["find_prev_vasp"],
  "budgets": { … }, "spent": { "api_calls": 20, "txs_normalized": 2572, "nodes": 205 },
  "engine_version": "…", "ruleset_version": "…", "error": null
}
```

`status` reaches one of `completed`, `partial` (a budget ran out and pursuit
could not or should not continue — the objectives were already answered, the
frontier was empty, or the extension ceiling was reached; the terminal finding
says which), or `failed` (`error` says why).

### `GET /investigations/{id}/findings` — results

Returns every finding with its full evidence chain, plus `answers` — the
server-side selection of what each objective can honestly be answered with:

```json
{
  "answers": [{
    "direction": "backward",
    "same": false,
    "nearest":       { "address": "0x…", "hop": 2, "confidence": 0.58, "named": false, "claim": null, "summary": "…" },
    "nearest_named": { "address": "0x…", "hop": 2, "confidence": 0.90, "named": true,  "claim": "OKX labeled 'vasp'", "summary": "…" }
  }],
  "findings": [ … ]
}
```

Two answers per direction on purpose: the closest endpoint and the closest
**named** one are different questions, and when they differ both are
reported. `same: true` means they are literally the same finding — show one
row. Selection lives server-side (`investigation/answers.py`) so every
consumer states the same thing.

### `GET /investigations/{id}/graph?limit=240&per_level=20` — the traversal

Nodes and edges for drawing, readable mid-run (the trace grows under the
poll). Amounts and `value_share` are decimal **strings** — smallest-unit
sums exceed 2^53 and a JSON number would round. `node_total` and `truncated`
report what a bounded read left out; `per_level` caps each (hop, direction)
group separately so a wide first hop cannot consume the whole budget and
delete the far hops from the picture.

## Service

- `GET /healthz` — liveness.
- `GET /metrics` — optional metrics endpoint.
- `GET /` — the demo frontend. `/?investigation={id}` reopens a stored
  trace; `&full=1` opens straight into the full-screen graph.

---

## Intel (approved, being built — LABEL_INTELLIGENCE.md §5)

All intel endpoints sit behind the API-key layer; none ship before it.

- `POST /intel/reports` — submit a suspected VASP address. Lands `pending`
  (quarantined): visible as unconfirmed intel, **never** able to name an
  investigation answer until corroborated. Attributable to the submitting key.
- `GET /intel/labels/stats` — label counts by tier/status/source, last
  refresh per source.
- `GET /intel/events?after=<cursor>` — label adds/promotions/retirements,
  oldest first; poll with the last seen id.
- `GET /investigations?status=…` — case list for the dashboard.
