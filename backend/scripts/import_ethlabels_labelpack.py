#!/usr/bin/env python
"""Import Etherscan's own exchange tags (via eth-labels) as a labelpack.

Etherscan publicly tags exchange-operated addresses — both operational wallets
("Binance 14", "Kraken 6") and, remarkably, tens of thousands of customer
deposit addresses ("Binance Dep: 0x…"). `dawsbot/eth-labels` republishes those
tags as a plain dataset, so no scraping is involved.

Provenance, stated honestly
---------------------------
This is a **third-party claim**, and a materially weaker one than the
proof-of-reserves pack:

- OKX's PoR addresses are self-attested — this repo verified 14,933 signatures
  itself, so the claim rests on cryptography.
- These tags rest on Etherscan's private methodology. Nobody outside Etherscan
  can check them, and there is no per-entry date.

They therefore ship at a LOWER confidence, and the two packs stay separate so a
reader can always tell which kind of evidence an answer rested on.

Licensing: the eth-labels repository is MIT, but that covers the repository and
its scraper — it does not itself license Etherscan's underlying tag data. Check
that before redistributing this pack outside your own use.

Cross-chain pooling
-------------------
A label row is accepted whichever `chainId` it was recorded under, then emitted
for Ethereum. Exchange wallets are EOAs, so the same address is the same
keyholder on every EVM chain — "Binance 14" is Binance's key regardless of
which chain's tag list happens to record it. This is load-bearing: Binance 14
(0x28c6c0…) is tagged ONLY under chainIds 480 and 43114, and an Ethereum-only
filter silently misses the single most-hit exchange wallet on Ethereum.

Usage
-----
    curl -L -o accounts.csv \\
      https://raw.githubusercontent.com/dawsbot/eth-labels/v1/data/csv/accounts.csv
    python scripts/import_ethlabels_labelpack.py \\
        --csv accounts.csv --out ../labels/exchanges-etherscan.json
"""

from __future__ import annotations

import argparse
import csv
import json
import re
import sys
from collections import Counter
from pathlib import Path
from typing import Any

# Exchanges whose tags we accept, mapped to a canonical entity name.
EXCHANGES = {
    "binance": "Binance",
    "okx": "OKX",
    "kraken": "Kraken",
    "bitget": "Bitget",
    "coinbase": "Coinbase",
    "kucoin": "KuCoin",
    "bybit": "Bybit",
    "gate.io": "Gate.io",
    "huobi": "Huobi",
    "htx": "HTX",
    "crypto.com": "Crypto.com",
    "bitfinex": "Bitfinex",
    "mexc": "MEXC",
    "gemini": "Gemini",
    "bitstamp": "Bitstamp",
}

# Tags that NAME an exchange but do not denote an exchange-controlled address.
# "Bybit Exploiter 33" is the thief, not Bybit — labelling it as the exchange
# would attribute stolen funds to the victim.
NOT_THE_EXCHANGE = re.compile(
    r"\b(exploiter|hacker|hack|drainer|scam|phish|fake|victim|attacker)\b", re.I
)

DEPOSIT = re.compile(r"\bdep\b\s*:", re.I)

# Known NON-custodial contracts. Emitted as a separate `infrastructure` pack so
# a behavioural detector cannot mistake a busy router for an exchange: a DEX
# settlement contract has the counterparty degree of a custodian and none of the
# custody. Selected from eth-labels' own curated protocol slugs plus a tight
# name pattern — deliberately not from free-text guessing.
INFRA_SLUGS = frozenset(
    {
        "dex",
        "mev-protection",
        "cow-protocol",
        "sushiswap",
        "uniswap",
        "curve",
        "balancer",
        "1inch",
        "0x-protocol",
        "paraswap",
        "aave",
        "compound",
        "lido",
        "maker",
        "bridge",
        "defi",
        "yearn",
        "convex",
        "pancakeswap",
        "dydx",
        "synthetix",
        "oracle",
        "nft-marketplace",
    }
)
INFRA_NAME = re.compile(r"\b(router|settlement|aggregator|proxy|relayer|vault|pool)\b", re.I)


def is_infrastructure(label_slug: str, name_tag: str) -> bool:
    return label_slug in INFRA_SLUGS or bool(INFRA_NAME.search(name_tag))


def classify(name_tag: str) -> tuple[str, str] | None:
    """(entity, role) for an exchange-controlled address, else None."""
    tag = name_tag.strip()
    lowered = tag.lower()
    if not tag or NOT_THE_EXCHANGE.search(lowered):
        return None
    for prefix, entity in EXCHANGES.items():
        if lowered.startswith(prefix):
            return entity, "deposit" if DEPOSIT.search(tag) else "operational"
    return None


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--csv", required=True, type=Path)
    parser.add_argument("--out", required=True, type=Path)
    parser.add_argument(
        "--infrastructure-out",
        type=Path,
        help="also write a pack of known non-custodial contracts (DEX/router/settlement)",
    )
    parser.add_argument(
        "--confidence",
        type=float,
        default=0.75,
        help="third-party tag; below the 0.9 used for signature-verified addresses",
    )
    parser.add_argument("--chain", default="ethereum")
    args = parser.parse_args()

    if not (0.0 < args.confidence < 1.0):
        print("error: confidence must be in (0, 1)", file=sys.stderr)
        return 2

    labels: dict[str, dict[str, Any]] = {}
    infrastructure: dict[str, dict[str, Any]] = {}
    roles: Counter[str] = Counter()
    skipped: Counter[str] = Counter()

    with args.csv.open() as handle:
        for row in csv.DictReader(handle):
            address = (row.get("address") or "").strip().lower()
            name_tag = (row.get("nameTag") or "").strip()
            if not address.startswith("0x"):
                continue
            if args.infrastructure_out and is_infrastructure(
                (row.get("label") or "").strip(), name_tag
            ):
                infrastructure.setdefault(
                    address,
                    {
                        "chain": args.chain,
                        "address": address,
                        "entity": name_tag or "non-custodial contract",
                        "category": "infrastructure",
                        "confidence": args.confidence,
                        "name_tag": name_tag,
                    },
                )
            verdict = classify(name_tag)
            if verdict is None:
                if name_tag and NOT_THE_EXCHANGE.search(name_tag):
                    skipped["names an exchange but is not the exchange"] += 1
                continue
            entity, role = verdict
            # First tag wins; rows repeat per chainId with the same tag.
            if address in labels:
                continue
            labels[address] = {
                "chain": args.chain,
                "address": address,
                "entity": f"{entity} ({role} address)",
                "category": "vasp",
                "confidence": args.confidence,
                "name_tag": name_tag,
                "role": role,
            }
            roles[role] += 1

    # An address can carry BOTH a pooled exchange tag ("OKX 213") and a
    # specific tag ("OKX: DEX Router 4", "Binance Pool"). One claim must win,
    # and the split is a ruling (2026-08-11), not import order:
    # - Routers/aggregators/proxies are NON-custodial: "funds reached OKX"
    #   concluded from a DEX router is exactly the false positive the
    #   infrastructure pack exists to prevent. Infrastructure wins.
    # - Exchange-operated POOLS are operated services that really receive
    #   funds. The VASP claim wins, and the address leaves the infra pack.
    # Non-exchange pools (no vasp claim to contest) stay infrastructure, so
    # the behavioural heuristic keeps its protection there.
    pool_tag = re.compile(r"\bpool\b", re.I)
    for address in sorted(labels.keys() & infrastructure.keys()):
        if pool_tag.search(infrastructure[address]["entity"]):
            winner, loser = "vasp", infrastructure.pop(address)
        else:
            winner, loser = "infrastructure", labels.pop(address)
        print(f"  contested   {address}: {winner} wins over {loser['entity']!r}")

    if not labels:
        print("nothing to import — refusing to write a pack", file=sys.stderr)
        return 1

    for reason, count in skipped.most_common():
        print(f"  skipped {count:>6}  {reason}")
    for role, count in roles.most_common():
        print(f"  imported {count:>5}  {role}")

    pack = {
        "source": "Etherscan public address tags, via dawsbot/eth-labels",
        "source_date": "2026-07-10",
        "method": "licensed_dataset",
        "license": "eth-labels repository is MIT; the underlying tags are Etherscan's",
        "_note": (
            "THIRD-PARTY CLAIM, weaker than the signature-verified proof-of-reserves pack: "
            "these tags rest on Etherscan's private methodology and cannot be independently "
            "checked, so they ship at lower confidence and in a separate pack. Label rows are "
            "pooled across chainIds and emitted for one chain, because exchange wallets are "
            "EOAs and the same address is the same keyholder on every EVM chain — Binance 14 "
            "is tagged only under chainIds 480/43114 yet is the most-hit Binance wallet on "
            "Ethereum. Tags naming an exchange as VICTIM (exploiter/hacker) are excluded."
        ),
        "default_confidence": args.confidence,
        "labels": sorted(labels.values(), key=lambda entry: entry["address"]),
    }
    args.out.write_text(json.dumps(pack, indent=1) + "\n")
    print(f"\nwrote {len(labels)} address(es) to {args.out}")

    if args.infrastructure_out and infrastructure:
        infra_pack = {
            "source": "Etherscan public address tags, via dawsbot/eth-labels",
            "source_date": "2026-07-10",
            "method": "licensed_dataset",
            "license": "eth-labels repository is MIT; the underlying tags are Etherscan's",
            "_note": (
                "Known NON-custodial contracts — DEX routers, settlement contracts, protocol "
                "proxies, bridges. This is not an accusation and not an endpoint: it exists so "
                "the behavioural service-endpoint heuristic cannot mistake a busy router for a "
                "custodian. A DEX settlement contract has the counterparty degree of an "
                "exchange and none of the custody."
            ),
            "default_confidence": args.confidence,
            "labels": sorted(infrastructure.values(), key=lambda e: e["address"]),
        }
        args.infrastructure_out.write_text(json.dumps(infra_pack, indent=1) + "\n")
        print(f"wrote {len(infrastructure)} non-custodial contract(s) to {args.infrastructure_out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
