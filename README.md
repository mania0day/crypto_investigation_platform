<div align="center">

# Crypto Investigation Platform

**Autonomous blockchain investigation — traces funds to the nearest exchange, and shows its working.**

You give it one address. It walks the money backward to where the funds came from and forward to where
they went, across hops, through mixers and obfuscation patterns, until it reaches a **VASP** — a
Virtual Asset Service Provider, i.e. an exchange — that a legal request can actually be served on.

Every conclusion carries the evidence it rests on. Every gap in coverage is stated out loud.

[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](LICENSE)
[![Python 3.12+](https://img.shields.io/badge/python-3.12+-blue.svg)](https://www.python.org/)
[![FastAPI](https://img.shields.io/badge/FastAPI-async-009688.svg)](https://fastapi.tiangolo.com/)
[![PostgreSQL](https://img.shields.io/badge/PostgreSQL-16-336791.svg)](https://www.postgresql.org/)
[![Tests](https://img.shields.io/badge/tests-1167%20passing-brightgreen.svg)](#testing)
[![mypy](https://img.shields.io/badge/mypy-strict-2a6db2.svg)](https://mypy-lang.org/)

</div>

---

## Contents

| | |
|---|---|
| [What it does](#what-it-does) · [Why it is different](#why-it-is-different) | the idea |
| [Quick start](#quick-start) · [Configuration](#configuration) | getting it running |
| [Usage manual](#usage-manual-a-to-z) | A to Z, every screen and every flag |
| [API reference](#api-reference) | every endpoint |
| [Architecture](#architecture) · [Repository layout](#repository-layout) | how it is built |
| [How data is gathered](#how-data-is-gathered) | providers and labels |
| [**Limitations**](#limitations-read-this) | **the data ceiling — read this** |
| [Testing](#testing) · [Licence](#licence) | the rest |

---

## What it does

### 1 · Start from one address

Paste an address, pick what you want to know, set a budget. The chain is detected automatically.

<div align="center"><img src="images/search.gif" alt="Starting an investigation" width="820"></div>

### 2 · Watch the trace build

The engine expands outward in both directions, claiming counterparties by value share. The picture is
written as it goes, so the graph grows while you watch it. Mixers, sweeps, peel chains and
consolidation patterns are detected as they are reached.

<div align="center"><img src="images/graph.gif" alt="The trace graph" width="820"></div>

### 3 · Get a report you can hand over

An investigation report as HTML or PDF: the money-in and money-out endpoints, the address to quote,
the confidence, the source each name rests on — and a full statement of everything the run did **not**
read.

<div align="center"><img src="images/report.gif" alt="The investigation report" width="820"></div>

---

## Why it is different

Most tools draw a graph and let you infer. This one separates **what is a fact** from **what is a
guess**, and refuses to blur them.

### The evidence taxonomy — four kinds, frozen

Every finding carries evidence, and every piece of evidence is exactly one of these:

| Kind | Means | Can it name an exchange? |
|---|---|---|
| `onchain_fact` | It is in the chain data. A transfer happened. | no |
| `heuristic_inference` | A rule fired on observed behaviour. | **no** |
| `third_party_claim` | A sourced label says so. | **yes — only this one** |
| `engine_observation` | The engine's own record of what it did. | no |

**Only a `third_party_claim` may name an operator.** An address that collects from 88 counterparties
and pays out to 27 *behaves* like custodial infrastructure — that is a `heuristic_inference`, and it
earns the words "service endpoint, operator unnamed" at ~60% confidence. It never earns the word
"Binance".

This is the whole design. A tool whose output may become a subpoena target does not get to guess at
who runs an address.

### It says what it did not read

A run that stops on a budget reports exactly what it left undone:

> Coverage is INCOMPLETE — this run did not read everything it reached: 76 address(es) read only in
> part; 1,464 address(es) queued but never explored; 12 high-degree address(es) expanded only in part;
> 657 onward branch(es) never followed.

An address that was never read cannot have been ruled out. Most tools draw what they got and let it
read as complete coverage.

### It chases the answer instead of stopping at the bill

If a run exhausts its budget with an objective still unanswered and work still queued, it raises the
budget and continues, up to a set number of extensions — because a run that stops on cost has not
answered the question, it has stopped paying for it.

**Depth is the exception, and deliberately so.** Spending more money is a cost decision; going deeper
changes what the trace *claims*. `max_depth` is never raised automatically.

---

## Quick start

**Requirements:** Node 18+, Python 3.12+, Docker. That is all — `start.sh` installs the rest.

```bash
git clone https://github.com/mania0day/crypto_investigation_platform.git
cd crypto_investigation_platform
./start.sh
```

It checks the toolchain, creates the Python venv, installs both npm trees, brings up Postgres, applies
migrations, imports ~75,000 labels, starts the services, and follows their logs.
**Ctrl-C stops everything it started.**

| | URL | |
|---|---|---|
| **Investigation UI** — start here | http://localhost:8000/ | this repository |
| **API docs** (OpenAPI) | http://localhost:8000/docs | this repository |
| Dashboard front end *(optional)* | http://localhost:5173/ | Abeera Zainab — see [NOTICE](NOTICE) |
| Dashboard API *(optional)* | http://localhost:4000/api/health | Abeera Zainab — see [NOTICE](NOTICE) |

> Everything shown in the screenshots and GIFs above is the investigation UI on **port 8000**
> (`backend/static/index.html`) — part of this project. The React dashboard on port 5173 is a
> separate, optional front end by another author and is not needed to run an investigation.

```bash
./start.sh --no-follow   # start, print the URLs, hand the terminal back
./start.sh --stop        # stop whatever a previous run started
./start.sh --no-install  # skip the dependency step on repeat runs

SERVER_PORT=4001 CLIENT_PORT=5174 BACKEND_PORT=8001 ./start.sh   # if a port is taken
```

Logs land in `logs/` — one file per service, plus `logs/install.log`.

<details>
<summary><b>Starting each piece by hand</b></summary>

```bash
cd backend
python3 -m venv .venv
.venv/bin/pip install -e '.[dev]'
./scripts/demo.sh          # postgres + migrations + labels + server on :8000
```

`demo.sh` mints a scoped API key and prints it. The bundled UI is served with that key embedded, which
is why it binds to `127.0.0.1` and nowhere else.

</details>

---

## Configuration

Everything is optional. The platform runs with **no keys at all** — TronGrid, Blockscout and the
public explorer tier are all keyless. Keys only raise rate limits and add fallbacks.

Copy `.env.example` to `.env` in the repository root:

```bash
ETHERSCAN_API_KEY=          # indexed history
TRONGRID_API_KEY=           # raises Tron from 2 to 5 requests/sec
DRPC_API_KEY=               # raw JSON-RPC pool
ANKR_API_KEY=
INFURA_API_KEY=
ALCHEMY_API_KEY=
# ... see .env.example for the full list

# AUTH_ENABLED=false        # local only. Defaults to ON.
```

> **Auth is on by default.** Every route except `/healthz` needs a key. Mint one with
> `python backend/scripts/manage_api_keys.py mint --scopes read,investigate`.
> Turning auth off is for a laptop, never a deployment.

The optional dashboard in `server/` needs its own `server/.env` with `BITCOIN_API_KEY`,
`ETHERSCAN_API_KEY` and `TRONGRID_API_KEY`. It is not required for the investigation engine.

---

## Usage manual (A to Z)

### A · Starting an investigation

**From the UI** — open http://localhost:8000/ and fill in the form:

| Field | What it means |
|---|---|
| **Address** | The subject. Chain is auto-detected by probing each candidate chain for history. |
| **Objectives** | `find_prev_vasp` (where funds came from) and/or `find_next_vasp` (where they went). At least one. |
| **Max depth** | How many hops out it may walk. **This is the setting that matters most** — see below. |
| **API call budget** | How many provider requests it may spend. |

**From the API:**

```bash
KEY='cc_….…'    # printed by start.sh, also in logs/backend.log

curl -s -X POST http://127.0.0.1:8000/investigations \
  -H "Authorization: Bearer $KEY" -H 'content-type: application/json' \
  -d '{
        "address": "TWtuyr5pSTUDXaLvZYbrt7WXry8fkNiT5A",
        "chain": "tron",
        "objectives": ["find_prev_vasp", "find_next_vasp"],
        "budgets": {"api_calls": 2000, "max_depth": 8, "max_nodes": 50000, "seconds": 7200}
      }'
```

### B · Choosing budgets — the part people get wrong

| Budget | Default | Raised automatically? | What it controls |
|---|---:|---|---|
| `max_depth` | 6 | **NEVER** | Hops from the subject. |
| `api_calls` | 100 | yes | Provider requests. |
| `seconds` | 300 | yes | Wall clock. |
| `max_nodes` | 500 | yes | Addresses held in the trace. |
| `max_extensions` | 8 | — | How many times the above may be raised. |
| `pursue_until_answered` | `true` | — | Turn off for a fixed, predictable spend. |

**There is no upper limit on any of them.** You can pass 5000.

> **The single most common mistake:** leaving `max_depth` low. Every other budget raises itself when
> an objective is unanswered, so a run with `max_depth: 3` will burn all 8 extensions exploring
> *wider at the same shallow depth*, spend everything, and stop having never looked further out.
> If a direction is not answering, **raise depth first.**

### C · Reading the graph

Left of centre is where the money came **from**; right is where it **went**. The subject sits in the
middle.

**Controls:** `Fit` · `−` / `+` zoom · `Expand frontier` (unfold the collapsed unexplored cards) ·
`Full screen` · `Minimize`.

**Node colours and marks**

| Mark | Meaning |
|---|---|
| Root | The address under investigation. |
| **Suspected VASP** | Behaves like an exchange. Unconfirmed — worth checking. |
| **Obfuscation pattern** | Peel chain, splitter, consolidation, relay or equal-value split. |
| *Unattributed* | The claim rests on inference only, with no sourced label. |
| `⋯` | History was read only in part. |
| `↩` | Returns to an address already placed at the same or a nearer hop. |
| Dashed edge | **Speculative** — the link crosses a mixer and may not hold. |

The graph is **bounded for legibility, not for coverage**. The note under it always states how many of
the total addresses are being drawn. Nodes carrying a sourced label or an answer are always drawn,
along with the path back to the root, so the address the answer names is never missing from the
picture.

### D · Reading the answer

Each direction reports up to two things, and both matter:

- **Nearest endpoint** — the closest thing to an exchange, which may be an unnamed behavioural
  inference at ~60%.
- **Nearest NAMED endpoint** — the closest one a source actually names, which may be further away
  but is the one you can serve a request on.

Reporting only one of these would either hide a named exchange behind a nearer guess, or hide the
nearer endpoint behind a further one.

If nothing can be named, it says so plainly: *"No endpoint in this direction carries a sourced label,
so no operator can be named."* It does not promote a guess to fill the space.

### E · The status banner

| Banner | Means |
|---|---|
| `Answered · coverage complete` | Objectives met, frontier ran dry. |
| `Answered · coverage partial` | Objectives met, but the run stopped with work queued. |
| `Partly answered · coverage partial` | One direction answered, one not. |
| `Not answered · coverage partial` | Stopped on a budget with nothing named. |

### F · Resuming a run

A run that stopped on a budget has status `partial` and can be picked up where it left off:

```bash
curl -s -X POST http://127.0.0.1:8000/investigations/<id>/resume \
  -H "Authorization: Bearer $KEY" -H 'content-type: application/json' \
  -d '{"budgets":{"api_calls":6000,"max_depth":10,"max_nodes":100000,"seconds":7200}}'
```

**Budgets must exceed what is already spent**, or the API refuses with a `422` naming what to raise —
because resuming on a spent budget would exhaust on the first check and write a second identical
partial result that looks like progress.

> **Only `partial` is resumable.** `completed` means the question is closed; `failed` needs diagnosis.
> If a server is killed mid-run the status stays `running` and the investigation is stranded — the
> data is safe (every node, edge and finding is written to Postgres as the engine expands, nothing is
> buffered), but you must set it back to `partial` to resume it.

### G · The report

Press **Download PDF** in the UI, or:

```bash
curl -s -H "Authorization: Bearer $KEY" \
  "http://127.0.0.1:8000/investigations/<id>/report?format=pdf" -o report.pdf
```

The report opens with a **money-in / money-out summary** — the operator name if one exists, the full
address to quote, hop distance, confidence, and the source the name rests on. Below that sit the
per-direction answers, every finding with its evidence, and a **Coverage and caveats** section stating
every gap in full.

`?format=html` returns the same document as a page. Both are printed from the same rendered HTML, so
the file in the case record cannot say something different from the page on screen.

### H · Reading confidence numbers

Confidence is a property of the *claim*, not a probability that you have caught someone.

| Range | Typically |
|---|---|
| 0.90 | A signature-verified proof-of-reserves label. The strongest thing here. |
| 0.75–0.95 | Behavioural patterns: sweep, peel, consolidation, relay. |
| 0.55–0.65 | Service-endpoint inference — "this behaves like an exchange", no name. |

A 90% named endpoint two hops away is worth more than a 63% unnamed one at one hop. That is why both
are reported.

---

## API reference

All routes except `/healthz` require `Authorization: Bearer <key>`.

| Method | Route | Scope | Purpose |
|---|---|---|---|
| `GET` | `/healthz` | — | Liveness. |
| `GET` | `/metrics` | `read` | Provider and engine counters. |
| `POST` | `/investigations` | `investigate` | Start a run. Returns the id immediately; the run continues in the background. |
| `GET` | `/investigations/{id}` | `read` | Status, budgets, spend, and every budget extension it granted. |
| `GET` | `/investigations/{id}/findings` | `read` | Every finding with its evidence, plus the selected answers. |
| `GET` | `/investigations/{id}/graph` | `read` | The traversal graph. Takes `?limit=` and `?per_level=`. Readable **while the run is going**. |
| `GET` | `/investigations/{id}/report` | `read` | The report. `?format=html\|pdf`. |
| `POST` | `/investigations/{id}/resume` | `investigate` | Continue a `partial` run on fresh budgets. |
| `GET` | `/` | — | The bundled single-file UI. |

Interactive docs at `/docs`.

---

## Architecture

<div align="center"><img src="images/architecture.png" alt="Full architecture" width="960"></div>

**The seams that matter:**

- **Chain SDK** is the *only* place that knows chain specifics. Everything above it works on a
  canonical model of addresses, transactions, movements and assets, so the engine is chain-agnostic.
- **Provider pool** routes by priority and fails over: keyed providers first, then the keyless API
  tier, then public explorer pages read at crawl speed. A trace that runs out of allowance should slow
  down, not stop.
- **Investigation engine** is a loop: claim → attribute → guard → expand → analyse → re-plan. It holds
  a frontier, a budget, and stopping conditions.
- **Evidence** is attached at the point a finding is made, never reconstructed afterwards.

<details>
<summary><b>Design concept — the full investigator workflow (not all built)</b></summary>

<br>

<div align="center"><img src="images/usage-concept.png" alt="Design concept" width="900"></div>

**This is a design document, not a description of the shipped software.** It shows the intended
end-state. Not yet built: pausing a running investigation, excluding or pinning branches, investigator
notes, and JSON/CSV/ZIP export. The shipped report formats are **HTML and PDF**.

</details>

---

## Repository layout

```
crypto_investigation_platform/
├── backend/                     the investigation engine — this is the project
│   ├── src/cipherchain/
│   │   ├── api/                 FastAPI app, routes, auth, schemas
│   │   ├── chains/              the ONLY place that knows chain specifics
│   │   │   ├── bitcoin/           UTXO adapter
│   │   │   ├── evm/               Ethereum, Polygon
│   │   │   ├── solana/
│   │   │   └── tron/
│   │   ├── providers/           pool, failover, rate limits, circuit breaker, cache
│   │   │   └── clients/           etherscan, evmrpc, blockscout, trongrid,
│   │   │                          mempoolspace, solanarpc, explorer_fetch
│   │   ├── investigation/       the engine: frontier, budgets, objectives, answers
│   │   ├── analysis/            heuristics, mixers, clustering, sanctions, attribution
│   │   ├── intel/               attribution policy — what may become a citable label
│   │   ├── harvest/             daily label harvest: OFAC SDN, proof-of-reserves
│   │   ├── reporting/           the report model, HTML and PDF rendering
│   │   ├── storage/             SQLAlchemy tables and repositories
│   │   ├── graph/               traversal graph construction
│   │   └── core/                canonical model, config, evidence taxonomy
│   ├── migrations/              Alembic
│   ├── scripts/                 demo.sh, harvest.sh, label import, key management
│   ├── static/index.html        the bundled single-file UI
│   └── tests/                   1167 tests
├── labels/                      address attribution packs (~75,000 rows)
├── assets/                      verified asset registry
├── bridges/                     cross-chain bridge reference data
├── docs/                        API reference, case study, research notes
├── images/                      README media
│
│   NOT this project's code — included so the stack runs end to end:
├── client/                      React landing/login/signup/dashboard — Abeera Zainab, see NOTICE
├── server/                      Express bitcoin/ethereum/tron routes — Abeera Zainab, see NOTICE
└── start.sh                     one command from a fresh clone to a running stack
```

---

## How data is gathered

Two entirely separate pipelines. Confusing them is the source of most misunderstanding about what this
tool can and cannot do.

### 1 · Chain data — live, per investigation

A provider pool with priority failover. Lower number wins; it falls through only when the one above
fails or spends its quota.

| Chain | Ladder |
|---|---|
| Ethereum / Polygon | Etherscan → RPC pool (dRPC, Ankr, Infura, Alchemy, QuickNode, Chainstack) → Blockscout → public explorer pages |
| Bitcoin | mempool.space → Blockscout |
| Tron | TronGrid → public explorer pages |
| Solana | Solana RPC |

Responses are cached, then normalised into a canonical model. Rate limits, retries and a circuit
breaker sit in front of every provider.

### 2 · Label data — batch, out of band

This is what lets the platform say a *name*, and it is completely separate from chain data.

- `labels/*.json` → `scripts/import_labelpacks.py` → the `labels` table
- `harvest/` — OFAC SDN sanctions (daily), exchange proof-of-reserves, Etherscan public tags
- `--drop-dir` for first-party sources that block automated download

**What is admitted as a citable label** is deliberately narrow — signature-verified, first-party
published, or licensed. Community lists are refused, and the reason is concrete: an entry like
`Binance (successor wallet 0xATTACKER)` would stem to "binance", promote against real Binance data,
and become an active, citable label attributing an attacker's wallet to an exchange. Untrusted sources
arrive `pending` and never become evidence.

---

## Limitations (read this)

Honest limits, measured rather than guessed. This section exists because a tool that hides these is
more dangerous than one that has them.

### The naming ceiling is the real limit — not compute

The engine can only name an operator it has a **sourced label** for. Measured coverage:

| Chain | Labels | Operators it can name |
|---|---:|---|
| Ethereum | 53,692 | **13** — Bitget, OKX, Binance, Coinbase, Kraken, HTX, KuCoin, Crypto.com, Bitstamp, Bybit, Bitfinex, MEXC, Gemini |
| **Tron** | 17,803 | **1 — OKX, and nothing else** |
| Bitcoin | 3,363 | OKX |
| Polygon | 70 | OKX |
| BSC, Arbitrum, Optimism, Avalanche, Solana | **0** | none |

**What this means in practice.** On a real Tron case this platform traced 6,113 addresses across
57,086 transactions, four hops in both directions. Labelled addresses found in the entire trace: **8,
all OKX, all in one direction.** The other direction reached 1,661 addresses and not one carried a
label.

Tripling the node count and adding a whole hop of depth bought **zero** new named operators. More
compute does not fix this. Only more label data does.

<details>
<summary><b>Why Tron coverage is hard, specifically</b></summary>

<br>

Attribution needs an address list from the exchange itself. Three situations:

- **OKX publishes one and signs it** — 17,803 Tron addresses. This is why the platform can name OKX.
- **Kraken, Bybit, KuCoin, Crypto.com** publish *Merkle attestations*: proof that they hold enough
  funds, without revealing which addresses. There is nothing in them to label.
- **Binance** publishes address information but blocks automated download. Working around a bot check
  is out of bounds here, so it needs a human to fetch the file once and drop it in `backend/drops/`.

Clustering does not rescue this either: the implemented co-spend heuristic is a **UTXO** technique
(several inputs signed in one transaction), and Tron is account-based. It helps Bitcoin, not Tron.

</details>

### Other honest limits

| Limit | Detail |
|---|---|
| **No pause** | A running investigation cannot be paused. It can be resumed after it stops on a budget. Stopping the server mid-run strands it at `running`. |
| **Coverage is usually partial** | Real traces fan out faster than any budget. The report states every gap; it does not pretend otherwise. |
| **Deposit vs reserve addresses** | Proof-of-reserves gives *reserve* wallets. It answers "these funds reached OKX" well, and "this is the deposit address customer funds sweep into" badly. |
| **High-degree addresses are sampled** | An address with 100+ counterparties follows the 20 largest by value. The rest are counted and reported as not followed. |
| **Mixer crossings are speculative** | The engine can continue past a mixer on a heuristic, but the link is marked speculative, drawn dashed, and barred from the headline answer. |
| **Label freshness** | Packs are point-in-time. The report prints the age of the claim each name rests on, so a stale attribution is visible rather than silent. |

### Closing the gap

| | Cost | Effect |
|---|---|---|
| Manual drop of a first-party address list into `backend/drops/` | minutes | The only free action with real Tron upside. |
| Wire more first-party proof-of-reserves sources | hours | Incremental. |
| Commercial attribution (Chainalysis, TRM, Elliptic) | paid | The real fix for deposit-address naming across chains. |

---

## Testing

```bash
cd backend
.venv/bin/python -m pytest -q          # 1167 tests
.venv/bin/ruff check src tests         # lint
.venv/bin/mypy                         # strict typing, 89 source files
```

Storage tests run against a **real Postgres 16 container** rather than a mock, because the queries
being tested are the product. The suite picks a free port per session, so concurrent runs are safe.

---

## Licence

**MIT** — see [LICENSE](LICENSE).

`client/` and `server/` are the work of **Abeera Zainab** and are credited in [NOTICE](NOTICE). The
MIT grant covers this repository's own code — the investigation engine in `backend/`, including the
port-8000 UI at `backend/static/index.html` that every screenshot here shows.

---

<div align="center">
<sub>Built by <a href="https://github.com/mania0day">mania0day</a></sub>
</div>
