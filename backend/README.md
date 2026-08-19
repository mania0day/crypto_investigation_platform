# CipherChain — Backend

Autonomous blockchain investigation engine. Given a wallet address, CipherChain traces the flow of funds backward (funding history) and forward (cash-out) and answers one question with evidence: **what is the nearest previous VASP and the nearest next VASP?**

The engine is the product; the graph is only its visualization. Design is governed by [`docs/01_PROJECT_VISION.md`](../docs/01_PROJECT_VISION.md) (frozen). This backend is the headless v1: engine + API + evidence-backed findings.

## Architecture at a glance

One bounded context per package; dependencies point inward toward `core`.

```
cipherchain/
  core/          canonical chain-agnostic model, evidence taxonomy, errors, settings, logging
  providers/     Provider SDK — capability-routed pool: cache → rate limit → retry → breaker, metrics
  chains/        Chain SDK — adapters that normalize chain data (bitcoin, evm)
  storage/       Postgres persistence: global fact store + per-investigation overlay
  investigation/ goal-directed engine: objectives, frontier, budgets, resumable state machine
  analysis/      Class F intelligence (consumers, never sources): attribution, sanctions, heuristics
  graph/         traversal / path reconstruction over stored investigation data
  api/           FastAPI surface
  runtime.py     composition root — where vendors, chains, and config meet
```

**The load-bearing invariant:** only chain adapters (through the provider pool) reach the outside world. The engine, analysis, and graph operate exclusively on normalized, stored data — enforced structurally (the engine holds no pool reference). The engine never branches on a chain's identity, so adding a chain never touches the engine.

## Status

| Area | State |
| --- | --- |
| Core model, evidence taxonomy | ✅ |
| Provider pool + clients (Etherscan V2, mempool.space, EVM JSON-RPC) | ✅ |
| Chain adapters | ✅ Bitcoin + Ethereum · ⬜ Tron (needs a free TronGrid key), Solana |
| Storage (10 tables, migrations, repositories) | ✅ |
| Investigation engine (2 objectives, 4 budgets, resumable) | ✅ |
| Analysis (OFAC sanctions, labelpack attribution, sweep heuristic) | ✅ |
| API (start / status / findings) | ✅ |
| Cross-chain bridge resolution, clustering, more chains | ⬜ planned |

Additional EVM chains are configuration, not code (one parameterized adapter). Attribution beyond OFAC sanctions (VASP labels) is supplied as operator labelpacks — CipherChain ships no invented exchange labels.

## Setup

Requires Python 3.12+ and Docker (for Postgres) or an external Postgres 16.

```bash
cd backend
python3 -m venv .venv
.venv/bin/pip install -e ".[dev]"

# credentials — every key is optional; the pool degrades to what's configured
cp ../.env.example ../.env   # then fill in keys (see docs/research/PROVIDER_INVENTORY.md)
```

Provider credentials live in `.env` at the repo root (gitignored). Roles and the free-tier inventory: [`docs/research/PROVIDER_INVENTORY.md`](../docs/research/PROVIDER_INVENTORY.md); the capability→vendor mapping: [`docs/research/CAPABILITY_MATRIX.md`](../docs/research/CAPABILITY_MATRIX.md).

## Database

```bash
# a throwaway dev database
docker run -d --name cipherchain-dev-pg \
  -e POSTGRES_USER=cipherchain -e POSTGRES_PASSWORD=cipherchain -e POSTGRES_DB=cipherchain \
  -p 127.0.0.1:54330:5432 postgres:16-alpine

export DATABASE_URL="postgresql+asyncpg://cipherchain:cipherchain@127.0.0.1:54330/cipherchain"
.venv/bin/python -m alembic upgrade head
```

## Run

**The demo — one command.** Starts Postgres, applies migrations, serves the API
and the web UI at <http://127.0.0.1:8000/>:

```bash
./scripts/demo.sh
```

Then open the page, paste an address (or use a preset), and press **Start
investigation**. The UI polls until the trace finishes and shows the answer to
the core query plus every finding with its evidence.

It binds to `127.0.0.1` deliberately: the API has no authentication and must not
be exposed off-machine.

Other entry points:

```bash
# acquisition data plane against real providers; run twice — the second run is
# served entirely from the durable cache (zero vendor calls)
DATABASE_URL=... .venv/bin/python scripts/demo_pipeline.py

# API only
DATABASE_URL=... .venv/bin/uvicorn cipherchain.api.app:create_app_from_settings --factory
```

## Naming what it finds

CipherChain can always *reach* an endpoint; it can only *name* one it has a label
for. Vendored OFAC sanctions data ships in the repo. Exchange/VASP labels are
operator-supplied — drop labelpacks in `labels/` (format and rationale in
[`labels/README.md`](../labels/README.md)). No invented labels are shipped: a
label is a claim about a real address.

Start an investigation:

```bash
curl -sX POST localhost:8000/investigations -H 'content-type: application/json' -d '{
  "chain": "ethereum",
  "address": "0x…",
  "objectives": ["find_prev_vasp", "find_next_vasp"]
}'
# → {"investigation_id": "…", "status": "running"}

curl localhost:8000/investigations/<id>
curl localhost:8000/investigations/<id>/findings
```

## Tests & quality gates

The full suite runs against a real Postgres 16 (a throwaway container is started automatically when Docker is available; set `CIPHERCHAIN_TEST_DATABASE_URL` to reuse an external one). Chain-adapter tests replay recorded real API payloads — no network needed.

```bash
.venv/bin/python -m pytest          # all tests
.venv/bin/python -m ruff check src tests
.venv/bin/python -m mypy            # strict
```

## Evidence posture

CipherChain is an evidence-first forensic architecture for investigative workflows. Every finding cites its support, classified and never conflated: on-chain fact, versioned heuristic inference, or dated third-party claim. Investigations are reproducible (engine + ruleset versions are recorded) and degrade gracefully — partial results state their gaps explicitly. CipherChain is decision support; it makes no claim of legal admissibility or certification.
