# Running the harvest on a 24/7 server

The engine answers investigations on demand. The **label store** behind those
answers is only as current as the last harvest cycle, and nothing refreshes it
by itself — that is what this directory sets up.

Why it matters is worth one paragraph, because the failure is silent. A trace
that reaches an address the store has no label for reports *no named endpoint*,
which reads like a fact about the chain and is actually a fact about the store.
For OFAC that reading is worse than unhelpful: an address designated last week
and absent here makes a trace through it report a **clean chain**.

## What runs

`scripts/harvest.sh` → `cipherchain.harvest.scheduler`, one cycle per
invocation, then exit. Not a resident loop and not a thread inside the API —
that would make "restart the API" and "skip a harvest" the same action, and
would run N times on N workers.

Each cycle records itself in `harvest_runs`, which is what the dashboard's
**Label sync** panel reads. That is the whole reason the table exists: a
process that exits between runs cannot be asked whether it is running.

| Source | Transport | Notes |
|---|---|---|
| Coinbase cbBTC reserves | automatic (drop fallback) | `robots.txt` permits it; page answers 200 |
| OFAC SDN | automatic (drop fallback) | ~28 MB, minutes — the long pole of the cycle |
| Binance PoR | **manual drop** | answers HTTP 202 with an empty body — a bot check |
| OKX PoR | **manual drop** | connection does not complete |

Binance and OKX will not become automatic. Getting past a bot check means
executing it or impersonating a browser, and that is the same boundary the
fetch tier holds (`providers/clients/explorer_fetch.py`). The drop path is not
a degraded mode — the file is still the exchange's own publication and it still
declares its own date.

## The Sync now button

The panel has one. It does **not** run the cycle inside the API — it spawns
`scripts/harvest.sh` as its own process, exactly as the timer does, and the
child writes the same run row. So a button-started cycle and a 03:15 cycle are
indistinguishable in the panel, because there is no second code path.

`POST /harvest/run` needs the INVESTIGATE scope, not READ: it spends bandwidth
and writes labels, which is the separation those scopes exist for. It answers
202 as soon as the child exists, and 409 if a cycle is already in flight —
refused rather than queued, because two concurrent reconciles over a
half-written harvest can promote a label on evidence the other transaction has
not committed.

The API claims the run row **before** spawning, and hands its id down in
`CIPHERCHAIN_HARVEST_RUN_ID`. Letting the child open its own row is the obvious
arrangement and it does not hold: the child needs seconds to boot Python and
reach the database, and a second press inside that window finds nothing open
and starts a second cycle. That was measured — two presses a second apart both
succeeded and both cycles ran.

## Install

Both units assume `/srv/cipherchain` and a `cipherchain` user. Adjust the paths
in the `.service` if yours differ.

```sh
sudo cp deploy/cipherchain-harvest.{service,timer} /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now cipherchain-harvest.timer

systemctl list-timers cipherchain-harvest.timer   # when it next fires
sudo systemctl start cipherchain-harvest.service  # run one now, don't wait
journalctl -u cipherchain-harvest.service -n 50   # what the last cycle said
```

## Reading the result

The exit code is the answer, and it is the same judgement the sync panel shows —
`scheduler._exit_code` computes it once and both read it.

| Code | Meaning | Do |
|---|---|---|
| 0 | every source that could contribute did | nothing |
| 1 | a source **broke**, or reconcile failed | read the journal; the rest still committed |
| 2 | misconfiguration — **nothing ran** | fix `DATABASE_URL`; no run row was even opened |
| 3 | nothing failed, and a publisher has gone quiet | go and look at that publisher |

A drop-only source nobody has ever supplied does **not** make this exit 1. It
reports as `AWAITING DROP:` in the summary and as *awaiting first drop* in the
sync panel. Binance and OKX will never fetch for themselves, so grading them as
failures would exit 1 every day forever on a deployment that has not done the
downloads — and a cron job that is red every morning is one nobody reads on the
morning something genuinely breaks.

The distinction the grading makes is *never supplied* versus *supplied and now
gone*. It is decided by whether that source has ever put a label in the store,
because an empty drop directory looks identical in both cases. A drop that was
working and has been deleted **is** a failure and does exit 1: coverage is
ageing silently from that moment, which is the condition this whole subsystem
exists to shout about.

`SuccessExitStatus=0 3` in the unit is deliberate. Exit 3 is a real signal, but
it is not a unit failure — treating it as one leaves the service permanently
red until somebody refreshes a drop. Exit 3 is meant to be seen in the sync
panel, which words it per source: a stale *fetched* source means the publisher
stopped, a stale *dropped* source means the drop directory is holding an old
file.

Exit 3 is also the easiest one to talk yourself out of. Coverage decays
quietly: a drop file stays on disk and re-ingests clean every morning, so a
publisher that stopped three weeks ago looks exactly like a healthy day.

## Doing a manual drop

On a machine that can reach the site, download the exchange's own published
file, then put it in `$CIPHERCHAIN_DROP_DIR` named for the source and dated
with the **publication date, not today's**:

```
binance-proof-of-reserves__2026-08-14.csv
okx-proof-of-reserves__2026-08-14.json
```

Leave old drops in place; the newest declared date wins. Dating a June file as
today does not make the coverage fresh — it only hides that it is not, from the
one alarm built to say so.
