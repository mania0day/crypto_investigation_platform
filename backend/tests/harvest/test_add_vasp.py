"""`scripts/add_vasp.py` — the supported way to add a VASP by hand.

The script's value is entirely in what it REFUSES. Everything it rejects is
something that, written by hand into `labels/*.json`, fails silently: the rows
load, sit in the table looking like coverage, and name nothing.
"""

from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path
from typing import Any

import pytest

from cipherchain.harvest.exchanges import BINANCE, OKX

SCRIPT = Path(__file__).resolve().parents[2] / "scripts" / "add_vasp.py"


def _module() -> Any:
    spec = importlib.util.spec_from_file_location("add_vasp", SCRIPT)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules["add_vasp"] = module
    spec.loader.exec_module(module)
    return module


add_vasp = _module()
TRON = "TQrY8tryqsYVCYS3MFbtffiPp2ccyn4STm"


def test_the_drop_source_table_matches_the_cycle_that_will_read_it() -> None:
    """DROP_SOURCES is restated in the script so it needs no httpx client to
    run. Restating is how two lists drift, so this is where they are pinned:
    a drop written under a name or method the harvester does not expect is
    refused on read, and the operator's download is wasted."""
    assert {
        BINANCE.name: BINANCE.method,
        OKX.name: OKX.method,
    } == add_vasp.DROP_SOURCES


@pytest.mark.parametrize(
    ("args", "because"),
    [
        (["--method", "community"], "community claims arrive pending and name nothing"),
        (["--confidence", "1.0"], "a claim is never proof"),
        (["--source-date", "14/08/2026"], "an undated claim cannot be judged for staleness"),
    ],
)
def test_it_refuses_what_would_fail_silently(
    tmp_path: Path, args: list[str], because: str
) -> None:
    base = [
        "--entity", "Binance", "--chain", "tron", "--source", "s",
        "--source-date", "2026-08-14", "--method", "first_party_published",
        "--address", TRON, "--out", str(tmp_path / "p.json"),
    ]
    # Appended, not substituted: argparse keeps the LAST value for a `store`
    # action, so this overrides the baseline whether or not the flag is in it.
    assert add_vasp.main(base + args) == 2, because
    assert not (tmp_path / "p.json").exists()


def test_an_address_from_the_wrong_chain_is_dropped_not_written(tmp_path: Path) -> None:
    """A tron address filed under ethereum is a row that can never match. It is
    reported and dropped rather than written with a caveat nobody reads."""
    out = tmp_path / "p.json"
    code = add_vasp.main([
        "--entity", "Binance", "--chain", "ethereum", "--source", "s",
        "--source-date", "2026-08-14", "--method", "first_party_published",
        "--address", TRON, "--address", "0x28c6c06298d514db089934071355e5743bf21d60",
        "--out", str(out),
    ])
    assert code == 0
    pack = json.loads(out.read_text())
    assert [row["address"] for row in pack["labels"]] == [
        "0x28c6c06298d514db089934071355e5743bf21d60"
    ]


def test_drop_mode_writes_what_the_harvester_will_accept(tmp_path: Path) -> None:
    """The two rules a hand-written drop gets wrong: `source` must equal the
    harvesting source's NAME (claim identity is chain+address+source, so
    otherwise a source could file under another's name and corroborate it), and
    `method` must match what that source is declared to use."""
    assert add_vasp.main([
        "--entity", "Binance", "--chain", "tron",
        "--source-date", "2026-08-14", "--method", "first_party_published",
        "--address", TRON,
        "--drop-for", BINANCE.name, "--drop-dir", str(tmp_path),
    ]) == 0
    written = tmp_path / f"{BINANCE.name}__2026-08-14.json"
    pack = json.loads(written.read_text())
    assert pack["source"] == BINANCE.name
    assert pack["method"] == BINANCE.method


def test_drop_mode_refuses_a_method_the_source_does_not_use(tmp_path: Path) -> None:
    """Caught here rather than at 03:15 tomorrow. A drop the harvester rejects
    on read is a download the operator made for nothing."""
    assert add_vasp.main([
        "--entity", "Binance", "--chain", "tron",
        "--source-date", "2026-08-14", "--method", "signature",
        "--address", TRON,
        "--drop-for", BINANCE.name, "--drop-dir", str(tmp_path),
    ]) == 2
    assert list(tmp_path.iterdir()) == []
