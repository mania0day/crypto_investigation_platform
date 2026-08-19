# RFC: Label Intelligence — from snapshot to living system

**Status: awaiting approval.** New bounded context; per process, the public
interface below is frozen only when explicitly approved.

## 1. The problem

CipherChain can only *name* an endpoint it has a label for, and labels today are a
snapshot: 74,939 records in `labels/*.json`, dated July/August 2026, loaded
once at startup. Nothing refreshes them, nothing accepts new ones, and nothing
tells the operator the dataset grew. Coverage is the platform's binding
constraint (00_PROJECT_STATE), and the snapshot is why.

## 2. Rulings locked on 2026-08-11

1. **Dashboard frontend is built fresh**, taking the reference platform's look
   but none of its code — the reference carries no license grant, and this
   platform is distributed to investigators. Same call as the graph view.
2. **Harvest sources are first-party and licensed only**: exchange
   proof-of-reserves disclosures (signature-verified where offered), OFAC
   updates, licensed datasets (eth-labels). Headless browser only where a
   first-party page is JS-rendered. No scraping against site terms; no
   credentialed automation; no bot-evasion.
3. **Community reports are quarantined until corroborated.** A submitted label
   is intel, not evidence: it never enters the attributor — and therefore can
   never name an answer — until an independent or first-party source agrees.
   *Refinement (user, same day): quarantined does not mean hidden.* Pending
   intel is shown wherever the address appears — dashboard, and as a marked
   annotation on trace views — always branded **UNCONFIRMED**, never entering
   evidence. And verification is a bot, not a queue: the harvester itself
   checks every pending report against active sources each cycle and promotes
   automatically, so nothing waits on a human.

## 3. Four-question gate

- **Why does it exist?** Coverage is the bottleneck, and labels rot. A label
  system that grows and re-verifies converts the platform's weakest property
  (a dated snapshot) into a compounding asset.
- **Where does it belong?** A new bounded context, `intel`, beside `analysis`.
  Attribution *reads* labels (already true); `intel` *acquires and verifies*
  them. The two never mix: acquisition is I/O and policy, attribution is pure.
- **Does it violate any principle?** It extends two. The provider-access
  invariant gets a sibling: **only `intel` source adapters touch the outside
  web** — the engine, detectors, and attributor never do. And the labels
  README rule ("no invented labels; a guessed label is a false accusation")
  becomes enforceable state: only `active` labels attribute.
- **What evidence proves it works?** The OKX pack is the existing proof: a
  first-party file, signature-verified, 36,049 addresses at 0.90, which named
  OKX on a real trace this week. The harvester is that pipeline made
  continuous and multi-source.

## 4. Design

### The label lifecycle (the core of the whole feature)

```
pending ──corroborated──▶ active ──source retracts/expires──▶ retired
   ▲  ▲                    │  │
   │  └────demoted─────────┘  └── ONLY active labels are loaded by the
   │      (basis stopped          attributor. pending/retired never
   │       holding)               attribute, never name, never rank.
   └── community report, unverified harvest find
```

Every record carries: source, source date, retrieval date, verification
method (`signature | first_party_published | licensed_dataset | community`),
tier confidence, and — when promoted — what corroborated it. The provenance
IS the product; a label an investigator cannot cite is worthless to them.

*Revised after adversarial review of the first implementation (2026-08-11),
which defeated the original one-way diagram three ways: promote honest
content then edit the row into something nobody corroborated; retire the one
source a promotion rested on and the echo survives it; and chain promoted
reports so two sock-puppets hold each other active.* The repairs, now
structural:

- **Active is continuously justified, never stamped.** An updated untrusted
  claim demotes immediately (whatever corroborated the old content did not
  corroborate the new — an honest edit loses one cycle, nothing more), and
  every reconcile cycle re-checks that each untrusted active claim still has
  a standing corroborator. Retiring a corroborator demotes its echoes one
  cycle later, on the record.
- **Only trusted-method claims corroborate.** A promoted community report is
  active, but activation is not trust: trust flows one way, from harvested
  sources to reports, never between reports.
- **Untrusted entities are names, not prose.** Stem matching ignores
  parentheticals and trailing indexes — annotation syntax in our packs, a
  smuggling channel in a report ("Binance (successor wallet 0xATTACKER)"
  stems to "binance" and promotes verbatim). Community entities: short,
  single-line, no parentheses, no URLs; role is its own field.
- **`demoted` is an event kind**, beside added/updated/promoted/retired.

### Storage

Labels move from startup-loaded files to a `labels` table; the existing
labelpack files become *import sources* (the operator drop-in workflow is
kept — files are ingested, not read live). A `label_events` append-only table
records every add / promotion / retirement with its reason, feeding both the
UI and an audit trail. The attributor loads `active` rows at startup and
re-reads incrementally when the harvester reports changes.

The store's claim identity is (chain, address, source): one source, one claim
per address. Importing the shipped packs surfaced 11 addresses carrying two
claims under one source — pooled exchange tags AND specific
router/pool tags — previously coexisting in the file loader and resolved in
the store by nothing but pack filename order. **Ruled 2026-08-11, an
intentional answer change:** the 8 DEX routers/aggregators/proxies are
infrastructure-only (a router is non-custodial, so "funds reached OKX"
concluded from one was a false positive), and the 3 exchange-operated mining
pools (Binance Pool, HTX Mining Pool, Huobi Mining Pool 2) stay VASP — an
operated pool really receives funds. The importer now refuses any claim
collision outright rather than letting order decide, and the pack generator
encodes the same split.

### The harvester

One asyncio worker in the API process (no new deployment unit). Per-source
refresh on a schedule with jitter; each cycle: fetch → verify (signatures
recover, checksums match, licenses unchanged) → diff against stored → write
adds/retirements as events. Failures degrade loudly per source — a source
that stops answering is reported in the UI, never silently skipped
(silent-degradation rule). Playwright is an optional dependency used only
for JS-rendered first-party pages; plain HTTP everywhere else.

### Community reports

`POST /intel/reports` accepts {chain, address, entity, category, evidence
URL, reporter contact (optional)}. Stored as `pending` with the reporter's
claim verbatim. Corroboration is checked automatically on every harvest
cycle: if an independent active source later covers the same address with a
compatible entity, the report promotes and the event says so. The dashboard
shows pending reports plainly marked as unverified intel.

One trap, named so it cannot be walked into: a pending report must NEVER
surface as `third_party_claim` evidence, even at low confidence — `is_named()`
keys on that evidence kind, so an unconfirmed report would silently become a
"named" answer. Pending intel reaches trace views as display-layer annotation
only ("1 unconfirmed report: Binance — UNVERIFIED"), never as evidence of any
kind. Promotion to `active` is what changes its evidence class, nothing else.

### KYC and VASP metadata

Stored per *entity* (exchange), not per address: jurisdiction, KYC regime,
each with a source URL and date — from the exchange's published policy. This
answers the investigator's actual question ("will a subpoena to this entity
work?") without the platform ever touching identity data of individuals.

## 5. Public interface (the freeze)

```
POST /intel/reports                   # community submission → pending
GET  /intel/labels/stats              # counts by tier/status/source, last refresh per source
GET  /intel/events?after=<cursor>     # label adds/promotions/retirements + harvester status
GET  /investigations?status=…&limit=… # case list for the dashboard (investigations context)
```

Events are polled with a cursor — the same pattern the UI already uses for
live traces — not pushed. Domain: `LabelRecord` gains `status`, `method`,
`retrieved_at`, `corroborated_by`. The attributor's read interface is
unchanged; the engine does not know this feature exists.

Every endpoint above sits behind the auth layer (§6). None of them exist
before it does.

## 6. Authentication and abuse — the gap this RFC first shipped with

Everything above assumed the platform's current posture could carry a public
write endpoint. It cannot. There is no authentication anywhere today — a
defensible gap while this was one investigator on 127.0.0.1, and an
indefensible one the moment strangers can write to the intel store or read
case material.

Two failure modes, named:

- **An unauthenticated report endpoint is a defamation printer.** Anyone
  could submit "0x… is Binance" and our own dashboard would publish the
  claim — branded UNCONFIRMED, but published — with nobody accountable for
  it. It is also a denial-of-service lever aimed at the corroboration cycle,
  which does real work per pending report.
- **A cases dashboard is case material.** Ongoing matters, victim addresses,
  sanctioned-entity tracing. Reading it is a privilege, not a default.

The rule this adds: **no public-facing surface ships before a minimal auth
layer exists.** Minimal means minimal — operator-issued API keys, sent as a
bearer header, checked against stored hashes; per-key rate limits on writes;
duplicate-report collapse. Every report is attributable to a key. Roles,
sessions, and user management are explicitly out of scope until a second
kind of user actually exists.

The harvester needs none of this: it has no inbound surface at all — it only
dials out. And the pre-existing investigation API keeps its local-bind
posture for now; once the key layer exists, retrofitting it is one
dependency, a decision for its own day rather than a rider on this one.

## 7. Frontend

One dashboard (fresh build, reference look): solved cases with their named
answers; a live intel side-panel animating harvester activity; themed popups
when labels land ("Binance PoR verified — 412 addresses added"); the report
form. All state from the four endpoints above.

## 8. What this will NOT do

No invented labels (unchanged, now structural). No scraping against site
terms. No credentialed sessions or bot-evasion. No KYC lookups on people.
No community claim ever names an answer while pending. No popup ever claims
more than its event: "added" is not "confirmed on-chain". No public surface
before auth (§6).

## 9. Build order (proposed)

storage (tables + migration) → intel domain + lifecycle → import existing
labelpacks → source adapters (OFAC, eth-labels, PoR incl. one new exchange)
→ harvester worker (internal-only: provably useful with zero exposure)
→ **auth layer** → reports endpoint + corroboration → events/stats API →
dashboard.

The line in the middle is the point: everything before the auth layer has no
inbound surface, so it can land and run with nothing at risk; nothing after
it ships without it. Each step lands tested before the next starts.
