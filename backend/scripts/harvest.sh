#!/usr/bin/env bash
# One harvest cycle: refresh every label source, then re-settle the lifecycle.
#
#   ./scripts/harvest.sh                     # uses ./drops
#   DROP_DIR=/var/lib/cipherchain/drops ./scripts/harvest.sh
#
# Daily, from cron — one process per day that exits, not a resident loop and
# NOT a thread inside the API. The API process serves investigations; a
# scheduler living inside it would make "restart the API" and "skip a harvest"
# the same action, and would run N times on N workers.
#
#   15 3 * * *  /srv/cipherchain/backend/scripts/harvest.sh >> /var/log/cipherchain/harvest.log 2>&1
#
# ── which sources update by themselves, and which need you ───────────────────
#
#   Coinbase   AUTOMATIC. The cycle fetches
#              https://www.coinbase.com/cbbtc/proof-of-reserves every run and
#              parses the addresses out of it. Nothing to do.
#   OFAC SDN   AUTOMATIC. The cycle fetches the published sanctions list
#              (sanctionslistservice.ofac.treas.gov) every run — ~28 MB, a few
#              minutes — so a newly designated address is in the store the next
#              morning without anybody being asked. This run takes longer than
#              the rest of the cycle put together; that is the download, not a
#              hang.
#   Binance    MANUAL. From a normal host that page answers HTTP 202 with an
#              empty body — a bot check. Working around it is out of bounds.
#   OKX        MANUAL. From a normal host okx.com does not connect at all.
#
# To do a manual drop: download the exchange's own published file on a machine
# that can reach the site, then put it in $DROP_DIR named for the source and
# dated with the file's PUBLICATION date, not today's —
#
#   binance-proof-of-reserves__2026-08-14.csv    a published proof-of-reserves file
#   binance-proof-of-reserves__2026-08-14.json   or a labelpack (labels/README.md)
#   okx-proof-of-reserves__2026-08-14.csv
#
# Leave old drops in place; the newest declared date wins. The date in the name
# is what a reader weighs the claim by AND what the staleness alarm reads, so
# dating a June file as today does not make the coverage fresh — it only hides
# that it is not. If this host ever loses its route to coinbase.com, the same
# drop path covers it: save the page as
# coinbase-cbbtc-reserves__<YYYY-MM-DD>.html and the cycle reads it as the same
# source. Same for the sanctions list: ofac-sdn__<YYYY-MM-DD>.xml, dated the
# Publish_Date inside the document.
#
# ── the two ways the SDN document itself can fail ────────────────────────────
#
# Neither ingests anything, and yesterday's rows are still standing after both.
# What differs is whether re-running is the job at all:
#
#   "the document stops part way through"
#              The download was cut off. Refusing the whole thing is deliberate
#              and not a bug to work around: a partial sanctions list parses
#              fine and would silently un-sanction every address past the cut.
#              Re-run it — tomorrow's cycle also retries by itself.
#   "not well-formed XML ... it did NOT stop mid-stream"
#              The whole body arrived and is broken. Re-running fetches the
#              same bytes and fails identically every morning, so open the
#              document and see what the publisher is serving (or what is
#              rewriting it in transit). This one does not fix itself.
#
# One more SDN line is worth reading rather than scrolling past: "row(s) filed
# on the ledger the address encodes, against the ticker". OFAC publishes rows
# whose currency ticker and address encoding contradict each other — one in 977
# as of 2026-08-18 — and the address wins, because a label filed on the wrong
# ledger can never match anything. Each such claim says so in its entity. One
# row is the publisher's typo; a jump to hundreds means the ticker field has
# changed meaning, and that is a person's decision rather than something for
# the parser to keep absorbing.
#
# Exit codes are the cron mail (see cipherchain/harvest/scheduler.py):
#   0  every source contributed a document its publisher still stands behind
#   1  a source contributed nothing, or reconcile failed — the rest committed
#   2  misconfiguration; nothing ran
#   3  nothing failed, and a source is still serving a document nobody has
#      republished inside its window. The line naming it starts with "STALE:"
#
# Exit 3 is the one that matters most and is the easiest to talk yourself out
# of. Coverage decays quietly: a drop file stays on disk and re-ingests clean
# every morning, so a publisher that stopped three weeks ago looks exactly like
# a healthy day — and CipherChain starts answering "no named endpoint" as though
# that were a fact about the chain rather than a gap in the label store.
set -euo pipefail

cd "$(dirname "$0")/.."
DROP_DIR="${DROP_DIR:-${CIPHERCHAIN_DROP_DIR:-drops}}"

if [ ! -x .venv/bin/python ]; then
  echo "error: .venv missing — run: python3 -m venv .venv && .venv/bin/pip install -e '.[dev]'" >&2
  exit 2
fi

# DATABASE_URL is left to the environment / .env on purpose: the harvester
# writes to the same label store the API reads, and a default invented here
# would be a second opinion about where that is.
exec .venv/bin/python -m cipherchain.harvest.scheduler --drop-dir "$DROP_DIR" "$@"
