"""The labelpack loader must find the operator's labels/ directory.

A wrong path here fails silently — the attributor just returns nothing and
every investigation honestly reports "no attributed endpoint", which looks
like the tool working rather than a misconfiguration.
"""

import json
from pathlib import Path

from cipherchain.analysis.attribution.loader import DEFAULT_LABELS_DIR, build_attributor
from cipherchain.core.models import Address
from cipherchain.investigation.attribution import CATEGORY_SANCTIONED, CATEGORY_VASP


def test_default_labels_dir_is_the_repo_root_labels_folder() -> None:
    # repo root holds .env, client/, docs/, backend/ — labels/ belongs there,
    # not inside backend/. Anchor on a sibling that must exist.
    assert DEFAULT_LABELS_DIR.name == "labels"
    assert (DEFAULT_LABELS_DIR.parent / "backend").is_dir(), DEFAULT_LABELS_DIR
    assert (DEFAULT_LABELS_DIR.parent / "client").is_dir(), DEFAULT_LABELS_DIR


def test_sanctions_load_by_default() -> None:
    attributor = build_attributor(labels_dir=Path("/nonexistent"))
    assert len(attributor) > 500  # vendored OFAC snapshot
    assert any("ofac" in name for name in attributor.source_names)


async def test_operator_labelpack_is_loaded_and_matched(tmp_path: Path) -> None:
    (tmp_path / "vasps.json").write_text(
        json.dumps(
            {
                "source": "test-pack",
                "source_date": "2026-08-07",
                "default_confidence": 0.8,
                "labels": [
                    {
                        "chain": "ethereum",
                        "address": "0xAAAAbbbb0000000000000000000000000000CCCC",
                        "entity": "Test Exchange",
                        "category": "vasp",
                    }
                ],
            }
        )
    )
    attributor = build_attributor(labels_dir=tmp_path)
    assert "test-pack" in attributor.source_names

    # the engine hands adapters' canonical (lowercase) form
    results = await attributor.attribute(
        Address("ethereum", "0xaaaabbbb0000000000000000000000000000cccc")
    )
    assert [(r.entity, r.category) for r in results] == [("Test Exchange", CATEGORY_VASP)]
    assert results[0].confidence == 0.8  # inherited default
    assert results[0].source_date is not None  # datable claim


async def test_sanctions_and_operator_labels_compose(tmp_path: Path) -> None:
    """Both sources answer through one attributor — a sanctioned address stays
    findable after an operator pack is added."""
    (tmp_path / "vasps.json").write_text(
        json.dumps({"source": "p", "source_date": "2026-08-07", "labels": []})
    )
    attributor = build_attributor(labels_dir=tmp_path)
    from cipherchain.analysis.sanctions import OfacSanctionsSource

    listed = next(r for r in OfacSanctionsSource().records() if r.chain == "ethereum")
    results = await attributor.attribute(Address("ethereum", listed.address.lower()))
    assert results and results[0].category == CATEGORY_SANCTIONED
