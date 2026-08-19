"""The production ingest path for all shipped labels, pinned.

Mutation testing found the import script executed by no test at all — a
hardcoded method, a dropped confidence, or a silently-resolved collision
would have shipped 74,939 labels wrong with a green suite.
"""

import importlib.util
import json
from pathlib import Path

import pytest

SCRIPT = Path(__file__).resolve().parents[2] / "scripts" / "import_labelpacks.py"


@pytest.fixture(scope="module")
def script():  # type: ignore[no-untyped-def]
    spec = importlib.util.spec_from_file_location("import_labelpacks", SCRIPT)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def write_pack(path: Path, *, method: str | None, labels: list[dict[str, object]]) -> Path:
    pack: dict[str, object] = {
        "source": "test-pack",
        "source_date": "2026-08-10",
        "default_confidence": 0.8,
        "labels": labels,
    }
    if method is not None:
        pack["method"] = method
    path.write_text(json.dumps(pack))
    return path


ROW = {"chain": "ethereum", "address": "0xaaa", "entity": "Acme", "category": "vasp"}


class TestPackClaims:
    def test_the_declared_method_is_the_stored_method(self, script, tmp_path: Path) -> None:  # type: ignore[no-untyped-def]
        """A pack declaring licensed_dataset must not arrive as anything
        stronger — the method decides the evidence class forever."""
        path = write_pack(tmp_path / "p.json", method="licensed_dataset", labels=[ROW])
        _, claims = script.pack_claims(path)
        assert [c.method for c in claims] == ["licensed_dataset"]
        assert claims[0].confidence == 0.8  # pack default applied

    def test_a_pack_without_a_trusted_method_is_refused_loudly(
        self,
        script,  # type: ignore[no-untyped-def]
        tmp_path: Path,
    ) -> None:
        """Silently importing it would land 74,939 labels PENDING — unable to
        name anything — which is quiet catastrophic degradation."""
        with pytest.raises(SystemExit, match="not a trusted tier"):
            script.pack_claims(write_pack(tmp_path / "p.json", method=None, labels=[ROW]))
        with pytest.raises(SystemExit, match="not a trusted tier"):
            script.pack_claims(write_pack(tmp_path / "q.json", method="community", labels=[ROW]))


class TestCollisionGuard:
    def test_cross_pack_collisions_are_refused_not_resolved_by_order(
        self,
        script,  # type: ignore[no-untyped-def]
        tmp_path: Path,
    ) -> None:
        """The live incident: same (chain, address, source) in two packs, the
        survivor picked by filename order. Never again silently."""
        a = write_pack(tmp_path / "a.json", method="licensed_dataset", labels=[ROW])
        b = write_pack(
            tmp_path / "b.json",
            method="licensed_dataset",
            labels=[{**ROW, "entity": "Acme Router", "category": "infrastructure"}],
        )
        packs = [(p, *script.pack_claims(p)) for p in (a, b)]
        with pytest.raises(SystemExit, match="collision"):
            script.refuse_claim_collisions(packs)

    def test_a_duplicate_inside_one_pack_is_also_a_collision(
        self,
        script,  # type: ignore[no-untyped-def]
        tmp_path: Path,
    ) -> None:
        path = write_pack(tmp_path / "a.json", method="licensed_dataset", labels=[ROW, dict(ROW)])
        packs = [(path, *script.pack_claims(path))]
        with pytest.raises(SystemExit, match="collision"):
            script.refuse_claim_collisions(packs)

    def test_disjoint_packs_pass(self, script, tmp_path: Path) -> None:  # type: ignore[no-untyped-def]
        a = write_pack(tmp_path / "a.json", method="licensed_dataset", labels=[ROW])
        b = write_pack(
            tmp_path / "b.json",
            method="licensed_dataset",
            labels=[{**ROW, "address": "0xbbb"}],
        )
        packs = [(p, *script.pack_claims(p)) for p in (a, b)]
        script.refuse_claim_collisions(packs)  # no raise

    def test_the_shipped_packs_are_collision_free(self, script) -> None:  # type: ignore[no-untyped-def]
        """The real labels/ directory must pass its own guard — this is the
        regression test for the 11-address incident and the ruling that
        resolved it (routers to infrastructure, pools to VASP)."""
        packs = [(p, *script.pack_claims(p)) for p in sorted(script.LABELS_DIR.glob("*.json"))]
        script.refuse_claim_collisions(packs)  # no raise
        total = sum(len(claims) for _, _, claims in packs)
        assert total == 74_928  # 74,939 minus the 11 double-tags, resolved
