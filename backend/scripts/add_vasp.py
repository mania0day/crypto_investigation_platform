#!/usr/bin/env python
"""Turn a list of addresses you can source into a labelpack CipherChain will name from.

This is the "I know these addresses belong to X" path. It exists because the
alternative people reach for — editing ``labels/*.json`` by hand — gets three
things wrong quietly: an address that is not valid for the chain it is filed
under (a label that can never match anything), a missing ``source_date`` (a
claim nobody can judge for staleness), and a method the policy does not trust
(74,000 labels' worth of precedent says that pack lands PENDING and names
nothing).

What this script will NOT do
----------------------------
Invent provenance. ``--source`` and ``--method`` are required and have no
defaults, because the whole difference between an attribution and an accusation
is who said it. Only three methods arrive able to name an operator
(:data:`cipherchain.intel.policy.TRUSTED_METHODS`):

    signature               the operator signed a message with the key. Strongest:
                            it survives the operator disappearing. Use
                            `import_por_labelpack.py`, which CHECKS the signature
                            rather than taking your word that one existed.
    first_party_published   the operator published this list themselves, and you
                            can point at where.
    licensed_dataset        a vendor you have a licence from published it.

Anything else arrives ``pending``: recorded, auditable, and unable to name an
endpoint until an independent trusted source agrees. That is deliberate. A
crowd-maintained entry reading `Binance (successor wallet 0xATTACKER)` stems to
"binance", promotes against real Binance data, and becomes a citable label — a
review demonstrated exactly that, which is why the tier exists at all.

Usage
-----
    # from a file of addresses, one per line, blank lines and # comments ignored
    python scripts/add_vasp.py \\
        --entity "Bitget" --chain tron \\
        --source "Bitget proof-of-reserves" \\
        --source-date 2026-08-14 \\
        --source-url https://www.bitget.com/promotion/proof-of-reserves \\
        --method first_party_published \\
        --addresses ~/Downloads/bitget-tron.txt \\
        --out ../labels/bitget-tron.json

    # then load it
    DATABASE_URL=... python scripts/import_labelpacks.py

For an exchange proof-of-reserves file that carries signatures, use
`import_por_labelpack.py` instead — it verifies each one and drops the rows that
fail, which this script cannot do because it is not given signatures.
"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import date
from pathlib import Path

from cipherchain.chains.bitcoin.adapter import _BECH32, _P2PKH_P2SH
from cipherchain.chains.evm.adapter import _EVM_ADDRESS
from cipherchain.chains.tron.adapter import _TRON_ADDRESS
from cipherchain.intel.policy import TRUSTED_METHODS

CATEGORIES = ("vasp", "sanctioned", "mixer", "infrastructure")
ROLES = ("deposit", "operational", "unknown")


def _recognizes(chain: str, address: str) -> bool:
    """Does this address belong to this chain's address space?

    The adapters' OWN compiled patterns, imported rather than restated. A pack
    whose addresses this script accepts and the engine later fails to match is
    worse than no validation at all: the rows sit in the store looking healthy
    and match nothing, so a trace that reaches the address still reports "no
    named endpoint". Importing the private names is deliberate — if one is
    renamed this fails loudly at import, where a copied regex would quietly
    drift out of agreement with the engine.

    Solana is absent because its addresses are bare base58 with no prefix and
    no length that distinguishes them from several other chains'; a pattern
    here would accept things the adapter does not. Pass Solana addresses and
    they are refused, which is the honest answer until the adapter exposes one.
    """
    value = address.strip()
    if chain in ("ethereum", "polygon"):
        return bool(_EVM_ADDRESS.match(value))
    if chain == "tron":
        return bool(_TRON_ADDRESS.match(value))
    if chain == "bitcoin":
        return bool(_P2PKH_P2SH.match(value) or _BECH32.match(value))
    return False


SUPPORTED_CHAINS = ("bitcoin", "ethereum", "polygon", "tron")

# The drop-only sources and the method each is declared to use. Kept here rather
# than imported from `harvest.exchanges` so this script needs no httpx client to
# run; the values are asserted against that module by the test suite, which is
# what keeps them from drifting.
DROP_SOURCES = {
    "binance-proof-of-reserves": "first_party_published",
    "okx-proof-of-reserves": "first_party_published",
}


def read_addresses(args: argparse.Namespace) -> list[str]:
    values: list[str] = list(args.address or [])
    if args.addresses:
        for raw in Path(args.addresses).read_text().splitlines():
            line = raw.split("#", 1)[0].strip()
            if line:
                values.append(line)
    # Order-preserving dedup: a published file often lists the same wallet under
    # several assets, and the pack should carry it once.
    seen: set[str] = set()
    unique = []
    for value in values:
        if value not in seen:
            seen.add(value)
            unique.append(value)
    return unique


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="add_vasp.py", description="Write a labelpack from addresses you can source."
    )
    parser.add_argument(
        "--entity", required=True, help='who the addresses belong to, e.g. "Bitget"'
    )
    parser.add_argument("--chain", required=True, choices=SUPPORTED_CHAINS)
    parser.add_argument(
        "--source",
        help="who says so — recorded on every claim. Required unless --drop-for sets it",
    )
    parser.add_argument("--source-date", required=True, help="the PUBLICATION date, YYYY-MM-DD")
    parser.add_argument("--source-url", help="where the claim can be checked")
    parser.add_argument(
        "--method", required=True, help=f"one of: {', '.join(sorted(TRUSTED_METHODS))}"
    )
    parser.add_argument("--category", default="vasp", choices=CATEGORIES)
    parser.add_argument("--role", default="operational", choices=ROLES)
    parser.add_argument("--confidence", type=float, default=0.8)
    parser.add_argument("--address", action="append", help="repeatable")
    parser.add_argument("--addresses", help="file with one address per line")
    parser.add_argument("--out", type=Path, help="write a labelpack here (labels/…)")
    parser.add_argument(
        "--drop-for",
        choices=sorted(DROP_SOURCES),
        help=(
            "write into the harvester's drop directory as this source instead, correctly "
            "named and dated — the supported way to supply Binance or OKX by hand"
        ),
    )
    parser.add_argument("--drop-dir", type=Path, default=Path("drops"))
    args = parser.parse_args(argv)

    if (args.out is None) == (args.drop_for is None):
        print("error: give exactly one of --out or --drop-for", file=sys.stderr)
        return 2
    if args.drop_for is None and not args.source:
        # Only --drop-for may supply it, and only because it has one correct
        # answer. Otherwise provenance is the operator's to state: the whole
        # difference between an attribution and an accusation is who said it.
        print("error: --source is required (or use --drop-for)", file=sys.stderr)
        return 2
    if args.drop_for is not None:
        # A drop must satisfy two rules the pack format enforces on read, and
        # both are silent traps when a pack is written by hand:
        #   * `source` must equal the harvesting source's NAME, because claim
        #     identity is (chain, address, source) — otherwise a source could
        #     file under another's name and then corroborate it.
        #   * `method` must equal what that source is declared to use.
        # Setting them here rather than trusting flags is the whole point of
        # this mode: a file the harvester refuses at 03:15 is a file whose
        # download was wasted.
        expected_method = DROP_SOURCES[args.drop_for]
        if args.method != expected_method:
            print(
                f"error: {args.drop_for} claims are {expected_method!r}; "
                f"--method {args.method!r} would be refused on read",
                file=sys.stderr,
            )
            return 2
        args.source = args.drop_for
        args.drop_dir.mkdir(parents=True, exist_ok=True)
        args.out = args.drop_dir / f"{args.drop_for}__{args.source_date}.json"

    if args.method not in TRUSTED_METHODS:
        # Refused rather than accepted-and-parked. A pack that silently lands
        # pending looks loaded — the rows are in the table — and names nothing,
        # and the operator finds out weeks later from a report that says "no
        # named endpoint" about a trace that reached an address they labelled.
        print(
            f"error: method {args.method!r} does not arrive active, so this pack would name "
            f"nothing. Use one of: {', '.join(sorted(TRUSTED_METHODS))}.\n"
            "If the addresses came from a community list, that is exactly the case the "
            "policy declines — see cipherchain/intel/policy.py.",
            file=sys.stderr,
        )
        return 2
    if not 0.0 < args.confidence < 1.0:
        print(
            "error: confidence must be strictly inside (0, 1) — a claim is never proof",
            file=sys.stderr,
        )
        return 2
    try:
        date.fromisoformat(args.source_date)
    except ValueError:
        print(f"error: --source-date {args.source_date!r} is not YYYY-MM-DD", file=sys.stderr)
        return 2

    addresses = read_addresses(args)
    if not addresses:
        print("error: no addresses given (--address or --addresses)", file=sys.stderr)
        return 2

    good: list[str] = []
    bad: list[str] = []
    for value in addresses:
        (good if _recognizes(args.chain, value) else bad).append(value)

    for value in bad:
        print(f"  dropped (not a valid {args.chain} address): {value}", file=sys.stderr)
    if not good:
        print(
            f"error: none of the {len(addresses)} addresses are valid for {args.chain}",
            file=sys.stderr,
        )
        return 1

    pack = {
        "source": args.source,
        "source_date": args.source_date,
        "method": args.method,
        "default_confidence": args.confidence,
        "labels": [
            {
                "chain": args.chain,
                "address": value,
                "entity": args.entity,
                "category": args.category,
                "role": args.role,
                "confidence": args.confidence,
                **({"source_url": args.source_url} if args.source_url else {}),
            }
            for value in good
        ],
    }
    args.out.write_text(json.dumps(pack, indent=1) + "\n")
    print(f"wrote {args.out}: {len(good)} label(s) for {args.entity} on {args.chain}", end="")
    print(f", {len(bad)} dropped" if bad else "")
    if args.drop_for is not None:
        print(
            "\nIt will be picked up by the next harvest cycle — or press Sync now on the "
            "dashboard.\nLeave older drops in place; the newest declared date wins."
        )
    else:
        print("\nload it with:\n  DATABASE_URL=... python scripts/import_labelpacks.py")
    return 0


if __name__ == "__main__":  # pragma: no cover - process entry point
    sys.exit(main())
