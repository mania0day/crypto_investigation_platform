"""Pick the demo case from evidence, not from memory.

For each candidate sanctioned address, ask Etherscan what it actually
transacted with, then intersect those counterparties with the mixer and VASP
label packs CipherChain already ships. A case only earns its place if the trace
really does touch both a mixer and a named VASP — asserting it from
recollection is how a demo turns out to be a story about a different address.

Deliberately does NOT import cipherchain: the engine is being rewritten right now.
Reads the key from .env and never prints it.
"""

from __future__ import annotations

import json
import pathlib
import sys
import time
from collections import Counter

import httpx

ROOT = pathlib.Path("/home/mania/Documents/crypto_investigation_project")
API = "https://api.etherscan.io/v2/api"


def load_key() -> str:
    for candidate in (ROOT / ".env", ROOT / "backend" / ".env"):
        if not candidate.exists():
            continue
        for line in candidate.read_text().splitlines():
            line = line.strip()
            if line.startswith("ETHERSCAN_API_KEY=") and len(line) > 18:
                return line.split("=", 1)[1].strip()
    sys.exit("no ETHERSCAN_API_KEY in .env")


def load_labels() -> tuple[dict[str, str], dict[str, str]]:
    def rows(path: pathlib.Path) -> list[dict]:
        data = json.loads(path.read_text())
        return data if isinstance(data, list) else data.get("labels", [])

    mixers: dict[str, str] = {}
    for r in rows(ROOT / "labels" / "verified-mixers.json"):
        if r.get("chain") == "ethereum":
            mixers[r["address"].lower()] = r.get("entity", "?")

    vasps: dict[str, str] = {}
    for name in ("verified-vasps.json", "exchanges-etherscan.json"):
        for r in rows(ROOT / "labels" / name):
            if r.get("chain") == "ethereum":
                vasps.setdefault(r["address"].lower(), r.get("entity", "?"))
    return mixers, vasps


def fetch(client: httpx.Client, key: str, action: str, address: str) -> list[dict]:
    params = {
        "chainid": "1",
        "module": "account",
        "action": action,
        "address": address,
        "startblock": "0",
        "endblock": "99999999",
        "page": "1",
        "offset": "3000",
        "sort": "asc",
        "apikey": key,
    }
    for attempt in range(4):
        try:
            r = client.get(API, params=params, timeout=45.0)
            body = r.json()
        except Exception as exc:
            if attempt == 3:
                print(f"    ! {action} failed: {type(exc).__name__}")
                return []
            time.sleep(2.0)
            continue
        if body.get("status") == "1" and isinstance(body.get("result"), list):
            return body["result"]
        msg = str(body.get("result") or body.get("message"))[:80]
        if "rate limit" in msg.lower() or "Max calls" in msg:
            time.sleep(2.0)
            continue
        return []
    return []


def main() -> None:
    key = load_key()
    mixers, vasps = load_labels()
    print(f"labels: {len(mixers)} eth mixer addrs, {len(vasps)} eth VASP addrs\n")

    ofac_file = ROOT / "backend/src/cipherchain/analysis/data/ofac_eth.json"
    ofac = [a.lower() for a in json.loads(ofac_file.read_text())]
    candidates = sys.argv[1:] or ofac

    results = []
    with httpx.Client() as client:
        for i, addr in enumerate(candidates, 1):
            normal = fetch(client, key, "txlist", addr)
            time.sleep(0.25)
            tokens = fetch(client, key, "tokentx", addr)
            time.sleep(0.25)

            counterparties = Counter()
            for row in list(normal) + list(tokens):
                for side in ("from", "to"):
                    v = (row.get(side) or "").lower()
                    if v and v != addr:
                        counterparties[v] += 1

            hit_mix = {a: mixers[a] for a in counterparties if a in mixers}
            hit_vasp = {a: vasps[a] for a in counterparties if a in vasps}
            total = len(normal) + len(tokens)
            results.append((addr, total, hit_mix, hit_vasp))

            flag = "  <== MIXER+VASP" if hit_mix and hit_vasp else ("  <- mixer" if hit_mix else "")
            print(
                f"[{i:3d}/{len(candidates)}] {addr}  txs={total:5d} "
                f"mixer={len(hit_mix):2d} vasp={len(hit_vasp):3d}{flag}"
            )
            if hit_mix:
                for a, e in list(hit_mix.items())[:6]:
                    print(f"          mixer: {e}  {a}")
            if hit_vasp:
                for a, e in list(hit_vasp.items())[:6]:
                    print(f"          vasp : {e}  {a}")

    print("\n" + "=" * 78)
    print("BEST CANDIDATES (touch a mixer AND a labeled VASP):")
    both = [r for r in results if r[2] and r[3]]
    both.sort(key=lambda r: (len(r[2]), len(r[3])), reverse=True)
    for addr, total, m, v in both[:10]:
        print(f"  {addr}  txs={total} mixers={len(m)} vasps={len(v)}")
        print(f"      mixers: {', '.join(sorted(set(m.values())))[:120]}")
        print(f"      vasps : {', '.join(sorted(set(v.values())))[:120]}")
    if not both:
        print("  none — widen the candidate set or reconsider the case")

    out = ROOT / "docs" / "research" / "case-probe-results.json"
    out.write_text(json.dumps(
        [{"address": a, "tx_count": t, "mixers": m, "vasps": v} for a, t, m, v in results],
        indent=2,
    ))
    print(f"\nwrote {out}")


if __name__ == "__main__":
    main()
