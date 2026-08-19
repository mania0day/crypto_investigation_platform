# Storage Schema — Review RFC

**Status:** ✅ APPROVED (2026-08-07). Rulings: Q1 — separate `assets` table (10th table, acknowledged). Q2 — table named `movements` (1:1 with the canonical model). Q3 — `provider_cache` stores both `raw` and `payload_json`.
**Scope:** exactly the core tables from the approved Phase-2 list, split per the frozen architecture into a **global immutable fact store** (shared across investigations; written once, never rewritten) and a **per-investigation overlay** (frontier state, annotations, findings). Postgres only, behind repositories; the `GraphStore` port reads these tables.

Conventions: `TIMESTAMPTZ` everywhere (UTC); amounts `NUMERIC(78,0)` (holds uint256; Python ints end-to-end, floats never touch value); SQLAlchemy constraint-naming convention so Alembic migrations are deterministic.

---

## Global immutable fact store

### `addresses`
| column | type | notes |
| --- | --- | --- |
| id | BIGSERIAL PK | |
| chain | TEXT NOT NULL | canonical chain id |
| address | TEXT NOT NULL | adapter-canonical form (EVM lowercased) |
| UNIQUE | (chain, address) | the identity |

### `transactions`
| column | type | notes |
| --- | --- | --- |
| id | BIGSERIAL PK | |
| chain | TEXT NOT NULL | |
| tx_hash | TEXT NOT NULL | |
| block_number | BIGINT NULL | |
| timestamp | TIMESTAMPTZ NOT NULL | temporal anchor for time-respecting traces |
| raw_sha256 | CHAR(64) NULL | content address of backing payload (links to `provider_cache`) |
| UNIQUE | (chain, tx_hash) | |

### `transfers` *(rows are canonical `Movement`s — see Q2 on naming)*
| column | type | notes |
| --- | --- | --- |
| id | BIGSERIAL PK | |
| transaction_id | BIGINT FK → transactions | |
| from_address_id | BIGINT NULL FK → addresses | NULL on UTXO output halves |
| to_address_id | BIGINT NULL FK → addresses | NULL on UTXO input halves |
| kind | TEXT NOT NULL | native / token / internal / utxo_input / utxo_output |
| asset\_\* | see Q1 | asset identity (inline columns or FK) |
| amount | NUMERIC(78,0) NOT NULL | smallest unit |
| index_in_tx | INT NOT NULL | stable identity within tx |
| timestamp | TIMESTAMPTZ NOT NULL | **denormalized** from transactions — traversal hot path filters by (address, time) without a join |
| provider | TEXT NOT NULL | provenance |
| retrieved_at | TIMESTAMPTZ NOT NULL | provenance |
| payload_sha256 | CHAR(64) NOT NULL | provenance — the digest findings cite |
| UNIQUE | (transaction_id, kind, index_in_tx) | idempotent re-normalization |
| INDEX | (from_address_id, timestamp), (to_address_id, timestamp) | backward/forward expansion queries |

### `assets` *(only if Q1 = separate table)*
| column | type | notes |
| --- | --- | --- |
| id | SERIAL PK | |
| chain | TEXT NOT NULL | |
| kind | TEXT NOT NULL | native / token |
| contract | TEXT NULL | NULL for native |
| symbol | TEXT NOT NULL | display only — never identity |
| decimals | INT NOT NULL | |
| UNIQUE NULLS NOT DISTINCT | (chain, kind, contract) | PG15+ |

### `provider_cache`
| column | type | notes |
| --- | --- | --- |
| cache_key | CHAR(64) PK | sha256 of (chain, capability, params) — never contains secrets |
| chain | TEXT NOT NULL | |
| capability | TEXT NOT NULL | |
| provider | TEXT NOT NULL | who answered |
| retrieved_at | TIMESTAMPTZ NOT NULL | TTL policies compare against this |
| payload_sha256 | CHAR(64) NOT NULL | digest of `raw` |
| raw | BYTEA NOT NULL | exact vendor bytes — evidence replay |
| payload_json | BYTEA (see Q3) | canonical JSON of the parsed payload |
| INDEX | (chain, capability) | |

---

## Per-investigation overlay (all rows `ON DELETE CASCADE` from investigations)

### `investigations`
| column | type | notes |
| --- | --- | --- |
| id | UUID PK (gen_random_uuid) | |
| root_address_id | BIGINT FK → addresses | created at intake |
| objectives | JSONB NOT NULL | v1: find_prev_vasp / find_next_vasp |
| status | TEXT NOT NULL | created / running / paused / completed / partial / failed |
| budgets | JSONB NOT NULL | api_calls, seconds, max_depth, max_nodes |
| spent | JSONB NOT NULL | same keys, updated at checkpoints |
| engine_version | TEXT NOT NULL | reproducibility (vision §4) |
| ruleset_version | TEXT NOT NULL | reproducibility |
| error | TEXT NULL | populated on failed |
| created_at / updated_at | TIMESTAMPTZ | |

### `nodes` — the checkpointed frontier (this is what makes investigations resumable)
| column | type | notes |
| --- | --- | --- |
| id | BIGSERIAL PK | |
| investigation_id | UUID FK CASCADE | |
| kind | TEXT NOT NULL | address / transaction (two-layer graph) |
| address_id | BIGINT NULL FK | set iff kind=address |
| transaction_id | BIGINT NULL FK | set iff kind=transaction |
| direction | TEXT NULL | backward / forward |
| hop_distance | INT NOT NULL | |
| value_share | NUMERIC NULL | frontier priority input |
| state | TEXT NOT NULL | frontier / expanded / terminal / excluded / pinned |
| discovered_reason | TEXT NOT NULL | the objective that caused this node — every fetch attributable (vision §2) |
| created_at | TIMESTAMPTZ | |
| UNIQUE NULLS NOT DISTINCT | (investigation_id, kind, address_id, transaction_id) | |
| CHECK | exactly one of address_id / transaction_id set | |

### `edges`
| column | type | notes |
| --- | --- | --- |
| id | BIGSERIAL PK | |
| investigation_id | UUID FK CASCADE | |
| src_node_id / dst_node_id | BIGINT FK → nodes | |
| transfer_id | BIGINT NULL FK → transfers | the fact this edge visualizes |
| kind | TEXT NOT NULL | movement / bridge |
| UNIQUE NULLS NOT DISTINCT | (investigation_id, src_node_id, dst_node_id, transfer_id) | |

### `findings`
| column | type | notes |
| --- | --- | --- |
| id | BIGSERIAL PK | |
| investigation_id | UUID FK CASCADE | |
| kind | TEXT NOT NULL | vasp_endpoint / sanctioned_address / mixer_interaction / bridge_crossing / sweep_pattern / terminal |
| subject_address_id | BIGINT NULL FK → addresses | |
| direction | TEXT NULL | |
| summary | TEXT NOT NULL | |
| confidence | DOUBLE PRECISION NOT NULL | (0,1] enforced in core model pre-insert |
| created_at | TIMESTAMPTZ | immutable once written (vision §4) |

### `evidence`
| column | type | notes |
| --- | --- | --- |
| id | BIGSERIAL PK | |
| finding_id | BIGINT FK CASCADE | |
| kind | TEXT NOT NULL | onchain_fact / heuristic_inference / third_party_claim |
| summary | TEXT NOT NULL | |
| refs | JSONB NOT NULL | tx hashes, payload digests, dataset ids |
| source | TEXT NULL | required by taxonomy for claims (validated in core) |
| source_date | TIMESTAMPTZ NULL | |
| heuristic | TEXT NULL | "name@version" for inferences |
| confidence | DOUBLE PRECISION NULL | NULL exactly for facts |

Taxonomy validity is enforced by `core.models.Evidence` before anything reaches the DB; the DB carries CHECKs only on enum-valued columns.

---

## Decision points

**Q1 — Asset identity on transfers:** separate `assets` table (+FK) or inline columns? Separate table is a 10th table beyond the frozen 9-list, but avoids repeating (chain, kind, contract, symbol, decimals) on millions of rows and gives assets a stable id the canonical asset registry can enrich. Recommendation: **separate table, acknowledged as an approved addition to the list.**

**Q2 — Table name:** your Phase-2 list says `transfers`; the frozen canonical model calls the row type `Movement`. Permanent naming friction between code and schema is a real cost. Recommendation: **rename the table `movements`** so schema and domain model are 1:1 (`transfers` was written before the UTXO-halves design existed).

**Q3 — `provider_cache` payload storage:** keep both `raw` (exact vendor bytes — evidence) and `payload_json` (canonical parse — what clients returned), or `raw` only (smaller, but cache hits must re-run client parsing, including multi-request payload assembly, which raw alone can't always reconstruct). Recommendation: **both columns.**
