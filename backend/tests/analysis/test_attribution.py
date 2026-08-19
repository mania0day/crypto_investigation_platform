"""Labelpacks carry provenance; the attributor indexes and ranks claims."""

import json
from pathlib import Path

import pytest

from cipherchain.analysis.attribution import (
    LabelStoreAttributor,
    load_labelpack,
    load_labelpack_dir,
)
from cipherchain.analysis.attribution.labels import LabelPack, LabelRecord, normalize_address
from cipherchain.core.errors import ConfigurationError
from cipherchain.core.models import Address


def write_pack(tmp_path: Path, name: str, payload: dict) -> Path:
    path = tmp_path / name
    path.write_text(json.dumps(payload))
    return path


class TestLabelpackFormat:
    def test_loads_with_provenance(self, tmp_path: Path) -> None:
        path = write_pack(
            tmp_path,
            "vasps.json",
            {
                "source": "acme-labels",
                "source_date": "2026-08-01",
                "default_confidence": 0.8,
                "labels": [
                    {
                        "chain": "ethereum",
                        "address": "0xABCdef0000000000000000000000000000000001",
                        "entity": "Acme Exchange",
                        "category": "vasp",
                    }
                ],
            },
        )
        pack = load_labelpack(path)
        assert pack.name == "acme-labels"
        record = pack.labels[0]
        assert record.entity == "Acme Exchange"
        assert record.confidence == 0.8  # inherited default
        assert record.source_date is not None and record.source_date.tzinfo is not None

    def test_source_is_mandatory(self, tmp_path: Path) -> None:
        path = write_pack(tmp_path, "bad.json", {"labels": []})
        with pytest.raises(ConfigurationError, match="provenance"):
            load_labelpack(path)

    def test_malformed_entry_names_its_index(self, tmp_path: Path) -> None:
        path = write_pack(
            tmp_path,
            "bad.json",
            {
                "source": "s",
                "source_date": "2026-08-01",
                "labels": [{"chain": "ethereum", "address": "0x1"}],
            },
        )
        with pytest.raises(ConfigurationError, match="entry 0"):
            load_labelpack(path)

    def test_undated_pack_is_refused(self, tmp_path: Path) -> None:
        """Ruling 3: operator data gets no free pass on provenance. An undated
        label would reach an evidence record undated, and a reader cannot weigh
        a claim without knowing how old it is."""
        path = write_pack(
            tmp_path,
            "undated.json",
            {
                "source": "someone",
                "labels": [
                    {"chain": "ethereum", "address": "0x1", "entity": "E", "category": "vasp"}
                ],
            },
        )
        with pytest.raises(ConfigurationError, match="source_date"):
            load_labelpack(path)

    def test_directory_loading_is_sorted_and_tolerates_absence(self, tmp_path: Path) -> None:
        write_pack(tmp_path, "b.json", {"source": "b", "source_date": "2026-08-01", "labels": []})
        write_pack(tmp_path, "a.json", {"source": "a", "source_date": "2026-08-01", "labels": []})
        assert [p.name for p in load_labelpack_dir(tmp_path)] == ["a", "b"]
        assert list(load_labelpack_dir(tmp_path / "missing")) == []


class TestAddressNormalization:
    def test_hex_is_lowercased_base58_is_untouched(self) -> None:
        assert normalize_address("0xAbCd") == "0xabcd"
        # Base58/Bech32 are case-sensitive: mangling them breaks lookups
        assert normalize_address("3PeVz6zCzRWsRq9YfZYbfbP92ZYDNyMUCC") == (
            "3PeVz6zCzRWsRq9YfZYbfbP92ZYDNyMUCC"
        )


class TestAttributor:
    def pack(self, *records: LabelRecord) -> LabelPack:
        return LabelPack(name="test", labels=records)

    async def test_case_insensitive_hex_lookup(self) -> None:
        attributor = LabelStoreAttributor(
            [
                self.pack(
                    LabelRecord(
                        chain="ethereum",
                        address="0xAAAA",
                        entity="Acme",
                        category="vasp",
                        source="s",
                        confidence=0.9,
                    )
                )
            ]
        )
        results = await attributor.attribute(Address("ethereum", "0xaaaa"))
        assert [r.entity for r in results] == ["Acme"]

    async def test_unknown_and_wrong_chain_return_nothing(self) -> None:
        attributor = LabelStoreAttributor(
            [
                self.pack(
                    LabelRecord(
                        chain="ethereum",
                        address="0xaaaa",
                        entity="Acme",
                        category="vasp",
                        source="s",
                        confidence=0.9,
                    )
                )
            ]
        )
        assert await attributor.attribute(Address("ethereum", "0xbbbb")) == ()
        assert await attributor.attribute(Address("bitcoin", "0xaaaa")) == ()

    async def test_multiple_claims_all_returned_confidence_first(self) -> None:
        attributor = LabelStoreAttributor(
            [
                self.pack(
                    LabelRecord(
                        chain="ethereum",
                        address="0xaaaa",
                        entity="Weak Claim",
                        category="vasp",
                        source="s1",
                        confidence=0.4,
                    )
                ),
                self.pack(
                    LabelRecord(
                        chain="ethereum",
                        address="0xaaaa",
                        entity="Strong Claim",
                        category="vasp",
                        source="s2",
                        confidence=0.95,
                    )
                ),
            ]
        )
        results = await attributor.attribute(Address("ethereum", "0xaaaa"))
        # every claim surfaces; none is silently discarded
        assert [r.entity for r in results] == ["Strong Claim", "Weak Claim"]

    def test_label_requires_valid_confidence(self) -> None:
        with pytest.raises(ValueError, match="confidence"):
            LabelRecord(
                chain="ethereum",
                address="0x1",
                entity="e",
                category="vasp",
                source="s",
                confidence=1.5,
            )
