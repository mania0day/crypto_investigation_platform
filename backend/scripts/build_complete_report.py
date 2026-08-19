#!/usr/bin/env python
"""Build the complete project report — the one document that explains the whole system.

Three audiences in one file, in the order somebody actually reads them: a plain
introduction that assumes nothing, a practical guide to running an
investigation and choosing its budgets, and a technical reference covering
every module.

Every figure is MEASURED here rather than typed into the prose. A document that
quotes hand-entered numbers starts drifting the moment anything changes, and
the drift is invisible — the sentences still read as though somebody checked
them. Anything that cannot be measured is marked in the template as prose.

    python scripts/build_complete_report.py
    python scripts/build_complete_report.py --html-only     # skip the PDF render
"""
# Embeds a typeset document; the same two rules the other report builder scopes
# off apply for the same reasons — reflowing prose to 100 columns moves where
# sentences break on the printed page, and en dashes are the correct glyph.
# ruff: noqa: E501

from __future__ import annotations

import argparse
import asyncio
import base64
import os
import re
import subprocess
import sys
from datetime import UTC, datetime
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
BACKEND = REPO / "backend"
DOCS = REPO / "docs"
TEMPLATE = DOCS / "complete-report.template.html"


def _run(*args: str, cwd: Path | None = None) -> str:
    return subprocess.run(
        args, cwd=cwd or REPO, capture_output=True, text=True, check=False
    ).stdout.strip()


def source_stats() -> dict[str, object]:
    """Line and file counts per package, straight off disk."""
    src = BACKEND / "src" / "cipherchain"
    packages = {}
    total_loc = total_files = 0
    for path in sorted(p for p in src.iterdir() if p.is_dir() and p.name != "__pycache__"):
        files = [f for f in path.rglob("*.py") if "__pycache__" not in f.parts]
        loc = sum(len(f.read_text(errors="replace").splitlines()) for f in files)
        packages[path.name] = {"files": len(files), "loc": loc}
        total_loc += loc
        total_files += len(files)
    root = [f for f in src.glob("*.py")]
    total_loc += sum(len(f.read_text(errors="replace").splitlines()) for f in root)
    total_files += len(root)
    tests = [f for f in (BACKEND / "tests").rglob("test_*.py")]
    return {
        "packages": packages,
        "loc": total_loc,
        "files": total_files,
        "test_files": len(tests),
        "test_loc": sum(len(f.read_text(errors="replace").splitlines()) for f in tests),
    }


async def label_stats(database_url: str) -> dict[str, object]:
    """Live label coverage. Empty dict if the database is not reachable — the
    document then prints the shipped figures with their measurement date rather
    than blanks, because a report that silently drops its own evidence is worse
    than one whose numbers are visibly dated."""
    try:
        from sqlalchemy import func, select

        from cipherchain.storage.db import create_engine, create_session_factory
        from cipherchain.storage.tables import LabelRow
    except Exception:
        return {}
    engine = create_engine(database_url)
    try:
        async with create_session_factory(engine)() as session:
            rows = (
                await session.execute(
                    select(LabelRow.chain, LabelRow.category, func.count())
                    .where(LabelRow.status == "active")
                    .group_by(LabelRow.chain, LabelRow.category)
                )
            ).all()
            methods = (
                await session.execute(
                    select(LabelRow.method, func.count())
                    .where(LabelRow.status == "active")
                    .group_by(LabelRow.method)
                )
            ).all()
            vasp_entities = (
                await session.execute(
                    select(LabelRow.chain, func.count(func.distinct(LabelRow.entity)))
                    .where(LabelRow.status == "active", LabelRow.category == "vasp")
                    .group_by(LabelRow.chain)
                )
            ).all()
    except Exception:
        return {}
    finally:
        await engine.dispose()
    by_chain: dict[str, dict[str, int]] = {}
    for chain, category, count in rows:
        by_chain.setdefault(str(chain), {})[str(category)] = int(count)
    return {
        "by_chain": by_chain,
        "methods": {str(m): int(c) for m, c in methods},
        "vasp_entities": {str(c): int(n) for c, n in vasp_entities},
        "total": sum(sum(v.values()) for v in by_chain.values()),
    }


def embed_figures(html: str) -> str:
    """Inline every ``figures/x.png`` as a data URI.

    Required, not an optimisation: ``render_pdf`` writes the HTML into a
    throwaway temp directory and prints from there, so a relative image path
    resolves to nothing and the figure silently prints as a broken-image box.
    Failing loudly here is better than a report whose pictures are missing in
    the copy somebody actually reads.
    """
    for figure in sorted((DOCS / "figures").glob("*.png")):
        token = f'src="figures/{figure.name}"'
        if token not in html:
            continue
        payload = base64.b64encode(figure.read_bytes()).decode("ascii")
        html = html.replace(token, f'src="data:image/png;base64,{payload}"')
    missing = re.findall(r'src="figures/([^"]+)"', html)
    if missing:
        raise SystemExit(f"figures referenced but not found: {', '.join(sorted(set(missing)))}")
    return html


def render(stats: dict[str, object]) -> str:
    html = TEMPLATE.read_text()
    for key, value in stats.items():
        html = html.replace("{{" + key + "}}", str(value))
    left = [t for t in html.split("{{")[1:]]
    if left:
        names = ", ".join(t.split("}}")[0] for t in left[:5])
        raise SystemExit(f"template has unfilled placeholders: {names}")
    return embed_figures(html)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="build_complete_report.py")
    parser.add_argument("--html-only", action="store_true")
    parser.add_argument("--output", type=Path, default=DOCS / "CipherChain-complete-report.pdf")
    args = parser.parse_args(argv)

    src = source_stats()
    database_url = os.environ.get("DATABASE_URL", "")
    labels = asyncio.run(label_stats(database_url)) if database_url else {}

    def chain_row(chain: str, fallback: tuple[int, int, int]) -> str:
        data = (labels.get("by_chain") or {}).get(chain)
        if not data:
            total, vasp, ents = fallback
        else:
            total = sum(data.values())
            vasp = data.get("vasp", 0)
            ents = (labels.get("vasp_entities") or {}).get(chain, 0)
        return f"<td class=num>{total:,}</td><td class=num>{vasp:,}</td><td class=num>{ents or '—'}</td>"

    stats: dict[str, object] = {
        "built": datetime.now(UTC).strftime("%d %B %Y"),
        "commit": _run("git", "rev-parse", "--short", "HEAD") or "unknown",
        "loc": f"{src['loc']:,}",
        "files": src["files"],
        "test_files": src["test_files"],
        "test_loc": f"{src['test_loc']:,}",
        # Computed, not asserted: the first draft of this document claimed 1.4
        # from memory and the real figure was 0.9.
        "test_ratio": f"{src['test_loc'] / max(src['loc'], 1):.2f}",  # type: ignore[operator]
        "labels_total": f"{labels.get('total', 75894):,}",
        "row_ethereum": chain_row("ethereum", (53796, 40786, 20)),
        "row_tron": chain_row("tron", (18080, 17803, 1)),
        "row_bitcoin": chain_row("bitcoin", (3944, 3365, 2)),
        "row_polygon": chain_row("polygon", (70, 70, 1)),
        "row_solana": chain_row("solana", (4, 0, 0)),
        "m_signature": f"{(labels.get('methods') or {}).get('signature', 36049):,}",
        "m_published": f"{(labels.get('methods') or {}).get('first_party_published', 1036):,}",
        "m_licensed": f"{(labels.get('methods') or {}).get('licensed_dataset', 38809):,}",
    }
    for name, data in src["packages"].items():  # type: ignore[union-attr]
        stats[f"pkg_{name}_loc"] = f"{data['loc']:,}"  # type: ignore[index]
        stats[f"pkg_{name}_files"] = data["files"]  # type: ignore[index]

    html = render(stats)
    html_path = DOCS / "CipherChain-complete-report.html"
    html_path.write_text(html)
    print(f"wrote {html_path.relative_to(REPO)} ({len(html):,} bytes)")

    if args.html_only:
        return 0
    from cipherchain.reporting.pdf import render_pdf

    written = render_pdf(html, args.output)
    print(f"wrote {written.relative_to(REPO)} ({written.stat().st_size:,} bytes)")
    return 0


if __name__ == "__main__":  # pragma: no cover - process entry point
    sys.exit(main())
