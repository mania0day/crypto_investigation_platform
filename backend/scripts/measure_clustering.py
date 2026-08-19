#!/usr/bin/env python
"""Measure what co-spend clustering would gain, before wiring it to anything.

Clustering changes which addresses can be NAMED, so it is a change to what
the system asserts. This script writes nothing: it reports how many clusters
the stored facts support, how many new addresses a sourced label would reach
through them, and — most importantly — what the guards refused.

The refusals are the interesting number. A run reporting large coverage and
zero CoinJoins skipped means the guard is not firing and the clusters should
not be trusted.

Usage:
    DATABASE_URL=postgresql+asyncpg://… python scripts/measure_clustering.py [--chain bitcoin]
"""

from __future__ import annotations

import argparse
import asyncio
import os
import sys
from collections import Counter, defaultdict

from sqlalchemy import text

from cipherchain.analysis.clustering import (
    SpendingTransaction,
    build_clusters,
    propose_cluster_labels,
)
from cipherchain.core.config import get_settings
from cipherchain.storage.db import create_engine, create_session_factory

# UTXO halves only: co-spend is meaningless on an account-model chain, where
# one signature moves one account's value and reveals nothing about others.
_MOVEMENTS = text(
    """
    SELECT t.tx_hash, m.kind, m.from_address_id, m.amount
    FROM movements m
    JOIN transactions t ON t.id = m.transaction_id
    WHERE t.chain = :chain AND m.kind IN ('utxo_input', 'utxo_output')
    """
)

_LABELLED_ADDRESSES = text(
    """
    SELECT a.id, l.entity, l.category, l.source, l.confidence
    FROM labels l
    JOIN addresses a ON a.chain = l.chain AND a.address = l.address
    WHERE l.status = 'active' AND a.chain = :chain
    """
)


async def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--chain", default="bitcoin")
    args = parser.parse_args()

    url = os.environ.get("DATABASE_URL") or get_settings().database_url
    engine = create_engine(url)
    factory = create_session_factory(engine)

    try:
        async with factory() as session:
            inputs: defaultdict[str, set[int]] = defaultdict(set)
            outputs: defaultdict[str, list[int]] = defaultdict(list)
            for tx_hash, kind, from_address_id, amount in (
                await session.execute(_MOVEMENTS, {"chain": args.chain})
            ).all():
                if kind == "utxo_input" and from_address_id is not None:
                    inputs[tx_hash].add(from_address_id)
                elif kind == "utxo_output":
                    outputs[tx_hash].append(int(amount))

            seeds: dict[int, tuple[str, str, str, float]] = {}
            mixers: set[int] = set()
            for address_id, entity, category, source, confidence in (
                await session.execute(_LABELLED_ADDRESSES, {"chain": args.chain})
            ).all():
                seeds[address_id] = (entity, category, source, float(confidence))
                if category == "mixer":
                    mixers.add(address_id)

        transactions = [
            SpendingTransaction(
                tx_hash=tx_hash,
                input_address_ids=tuple(sorted(spenders)),
                output_amounts=tuple(outputs.get(tx_hash, ())),
            )
            for tx_hash, spenders in inputs.items()
        ]
        multi = [t for t in transactions if len(t.distinct_inputs) > 1]

        print(f"chain                    {args.chain}")
        print(f"transactions in store    {len(transactions)}")
        print(f"  multi-input            {len(multi)}")
        print(f"labelled addresses       {len(seeds)} (of which mixer: {len(mixers)})")

        report = build_clusters(multi, mixer_address_ids=frozenset(mixers))
        print("\n-- clustering --")
        print(f"clusters                 {len(report.clusters)}")
        print(f"addresses clustered      {report.clustered_addresses}")
        if report.clusters:
            sizes = sorted((len(c) for c in report.clusters), reverse=True)
            print(f"largest cluster          {sizes[0]}")
            print(f"size distribution        {Counter(sizes).most_common(6)}")
        print(f"REFUSED coinjoin-shaped  {report.coinjoins_skipped}")
        print(f"REFUSED mixer-touching   {report.mixer_transactions_skipped}")

        proposals, conflicts = propose_cluster_labels(report, seeds)
        gained = sum(len(p.new_address_ids) for p in proposals)
        print("\n-- coverage gain --")
        print(f"clusters with a seed     {len(proposals)}")
        print(f"NEW addresses named      {gained}")
        if seeds:
            print(f"  as % of existing       {gained / len(seeds):.1%}")
        for proposal in sorted(proposals, key=lambda p: -len(p.new_address_ids))[:10]:
            print(
                f"  {proposal.entity:<28} +{len(proposal.new_address_ids):<5}"
                f" from {len(proposal.seed_address_ids)} seed(s)"
                f"  conf {proposal.confidence:.2f}"
            )
        if conflicts:
            print(f"\nREFUSED conflicted       {len(conflicts)}")
            for pair in conflicts[:10]:
                print(f"  {' vs '.join(pair)}")
        return 0
    finally:
        await engine.dispose()


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
