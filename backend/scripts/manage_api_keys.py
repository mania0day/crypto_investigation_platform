#!/usr/bin/env python
"""Mint, list and revoke CipherChain API keys.

The keys this issues are the only thing standing between a non-local bind and
an open investigation console (see ``cipherchain.api.auth``). ``scripts/demo.sh``
binds 127.0.0.1 because that used to be the whole access control; a host that
serves the API anywhere else must run with ``AUTH_ENABLED`` on and hand every
caller a key minted here.

Usage:
    DATABASE_URL=postgresql+asyncpg://…  python scripts/manage_api_keys.py <cmd>

    mint --scopes read,investigate --label "Det. Ruiz, case 2026-114"
    list
    revoke cc_1a2b3c4d5e6f7a8b

Scopes do not imply one another: a key that starts investigations AND reads
their results needs both ``read`` and ``investigate``. ``read`` alone is the
right default for a dashboard or a reviewing analyst — starting a trace spends
provider quota and minutes of work.

The secret is printed exactly once, by ``mint``, and is not recoverable
afterwards: only a scrypt digest is stored. A lost secret is replaced by
revoking the key and minting another, never by looking it up.
"""

from __future__ import annotations

import argparse
import asyncio
import os
import sys
from collections.abc import Sequence

from cipherchain.api.auth import (
    RevokeOutcome,
    Scope,
    format_scopes,
    list_keys,
    mint_key,
    revoke_key,
)
from cipherchain.core.config import get_settings
from cipherchain.storage.db import create_engine, create_session_factory


def parse_args(argv: Sequence[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog="manage_api_keys.py",
        description="Mint, list and revoke CipherChain API keys.",
    )
    commands = parser.add_subparsers(dest="command", required=True)

    mint = commands.add_parser("mint", help="issue a new key and print its secret once")
    mint.add_argument(
        "--scopes",
        default=str(Scope.READ),
        help=(
            "comma- or space-separated scopes "
            f"({', '.join(str(s) for s in Scope)}); default: {Scope.READ}"
        ),
    )
    mint.add_argument(
        "--label",
        default=None,
        help="who or what this key is for — it is the only way to tell keys apart later",
    )

    commands.add_parser("list", help="show every issued key (no secrets, no digests)")

    revoke = commands.add_parser("revoke", help="kill a key; effective on its next request")
    revoke.add_argument("key_id", help="the public key id, e.g. cc_1a2b3c4d5e6f7a8b")

    return parser.parse_args(argv)


def _scopes_from_flag(raw: str) -> list[str]:
    names = [part for part in raw.replace(",", " ").split() if part]
    known = {str(scope) for scope in Scope}
    unknown = [name for name in names if name not in known]
    if unknown:
        # Refuse rather than mint: a typo'd scope is a key that silently cannot
        # do the job it was issued for, discovered later by a 403 in production.
        raise SystemExit(
            f"unknown scope(s): {', '.join(sorted(unknown))} — known scopes are "
            f"{', '.join(sorted(known))}"
        )
    if not names:
        raise SystemExit("a key with no scopes can do nothing — pass --scopes")
    return names


def _print_minted(key_id: str, token: str, scopes: str, label: str | None) -> None:
    print()
    print(f"  key id : {key_id}")
    print(f"  scopes : {scopes}")
    print(f"  label  : {label or '(none)'}")
    print()
    print("  Send this token as an Authorization header:")
    print()
    print(f"    Authorization: Bearer {token}")
    print()
    print("  THIS SECRET IS SHOWN ONCE. It is stored only as a scrypt digest, so")
    print("  nothing — not this tool, not the database, not an administrator —")
    print("  can recover it. If it is lost, revoke this key and mint another.")
    print()


async def run(args: argparse.Namespace) -> int:
    url = os.environ.get("DATABASE_URL") or get_settings().database_url
    engine = create_engine(url)
    factory = create_session_factory(engine)
    try:
        async with factory() as session:
            if args.command == "mint":
                scopes = _scopes_from_flag(args.scopes)
                minted = await mint_key(session, scopes, label=args.label)
                _print_minted(
                    minted.key_id, minted.token, format_scopes(minted.scopes), minted.label
                )
                return 0
            if args.command == "list":
                keys = await list_keys(session)
                if not keys:
                    print("no API keys issued yet — mint one with: manage_api_keys.py mint")
                    return 0
                print(f"{'key id':<22} {'state':<9} {'scopes':<24} label")
                for key in keys:
                    state = "active" if key.active else "revoked"
                    print(
                        f"{key.key_id:<22} {state:<9} "
                        f"{format_scopes(key.scopes):<24} {key.label or ''}"
                    )
                return 0
            outcome = await revoke_key(session, args.key_id)
            if outcome is RevokeOutcome.REVOKED:
                print(f"revoked {args.key_id} — it stops working on its next request")
                return 0
            if outcome is RevokeOutcome.ALREADY_REVOKED:
                print(f"{args.key_id} was already revoked; nothing to do")
                return 0
            print(f"no such key: {args.key_id}", file=sys.stderr)
            return 1
    finally:
        await engine.dispose()


def main(argv: Sequence[str] | None = None) -> int:
    return asyncio.run(run(parse_args(argv)))


if __name__ == "__main__":
    sys.exit(main())
