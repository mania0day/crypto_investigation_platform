"""The bridge from lifecycle store to attributor, and the migration's anchor:
labels that moved from files into the store must attribute IDENTICALLY.

If this equivalence breaks, the import changed answers — the one thing the
ruling said it must not do."""

import json
from datetime import UTC, datetime
from pathlib import Path

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from cipherchain.analysis.attribution.labels import load_labelpack_dir
from cipherchain.analysis.attribution.store import LabelStoreAttributor
from cipherchain.core.models import Address
from cipherchain.intel.attributor_source import StoredLabelSource, build_store_attributor
from cipherchain.intel.policy import IntelClaim
from cipherchain.intel.service import IntelService
from cipherchain.storage.repositories import LabelRepository, StoredLabel

T0 = datetime(2026, 8, 11, 12, 0, tzinfo=UTC)

PACK = {
    "source": "acme proof-of-reserves, signature-verified",
    "source_date": "2026-08-10",
    "method": "signature",
    "license": "test",
    "default_confidence": 0.9,
    "labels": [
        {
            "chain": "ethereum",
            "address": "0xAbCd000000000000000000000000000000000001",
            "entity": "Acme (operational address)",
            "category": "vasp",
            "confidence": 0.9,
            "role": "operational",
        },
        {
            "chain": "tron",
            "address": "TAcmeCaseSensitive1111111111111111",
            "entity": "Acme (deposit address)",
            "category": "vasp",
            "confidence": 0.85,
            "role": "deposit",
        },
    ],
}


def stored_label(**overrides: object) -> StoredLabel:
    base: dict[str, object] = {
        "id": 1,
        "chain": "ethereum",
        "address": "0xaaa",
        "entity": "Acme (operational address)",
        "category": "vasp",
        "role": "operational",
        "confidence": 0.9,
        "status": "active",
        "method": "signature",
        "source": "acme-por",
        "source_date": T0,
        "retrieved_at": T0,
        "corroborated_by": None,
        "evidence_url": None,
        "reporter": None,
    }
    base.update(overrides)
    return StoredLabel(**base)  # type: ignore[arg-type]


class TestStoredLabelSource:
    def test_the_citation_is_the_original_source_not_the_store(self) -> None:
        (record,) = list(StoredLabelSource([stored_label()]).records())
        assert record.source == "acme-por"
        assert record.role.value == "operational"
        assert record.source_date == T0

    async def test_an_empty_store_still_screens_sanctions(
        self, sessions: async_sessionmaker[AsyncSession]
    ) -> None:
        """Loud degradation, not blindness: naming is off, OFAC is not."""
        attributor = await build_store_attributor(sessions)
        assert "ofac" in " ".join(attributor.source_names).lower()

    async def test_pending_rows_never_reach_the_production_attributor(
        self, sessions: async_sessionmaker[AsyncSession]
    ) -> None:
        """Mutation testing proved this unpinned: loading pending rows too
        passed the whole suite. This is the enforcement point of the entire
        no-false-positives contract — pin it at the production builder."""
        async with sessions() as session:
            await IntelService(session).ingest(
                IntelClaim(
                    chain="ethereum",
                    address="0x000000000000000000000000000000000000cafe",
                    entity="Unverified Exchange",
                    category="vasp",
                    role="unknown",
                    confidence=0.4,
                    method="community",
                    source="report:key-1",
                    retrieved_at=T0,
                )
            )
            await session.commit()
        attributor = await build_store_attributor(sessions)
        hit = await attributor.attribute(
            Address(chain="ethereum", value="0x000000000000000000000000000000000000cafe")
        )
        assert hit == (), "a pending community report must never attribute"

    async def test_a_harvest_reaches_a_server_that_is_already_running(
        self, sessions: async_sessionmaker[AsyncSession]
    ) -> None:
        """Without this the whole harvest subsystem is write-only.

        The index is built once at startup, so a daily harvest landed in the
        `labels` table and a long-running server never saw it: it answered with
        week-old attribution and stamped today's date on it. The failure is
        invisible, because a label that is merely out of date looks exactly like
        a label that is absent — and "no named endpoint" is a conclusion an
        investigator acts on.
        """
        address = Address(chain="ethereum", value="0x00000000000000000000000000000000000000ff")
        attributor = await build_store_attributor(sessions, check_interval=0.0)
        assert await attributor.attribute(address) == (), "not labelled yet"

        # the harvester runs while the server stays up
        async with sessions() as session:
            await IntelService(session).ingest(
                IntelClaim(
                    chain="ethereum",
                    address="0x00000000000000000000000000000000000000ff",
                    entity="Freshly Harvested Exchange",
                    category="vasp",
                    role="operational",
                    confidence=0.9,
                    method="first_party_published",
                    source="acme-por",
                    source_date=T0,
                    retrieved_at=T0,
                )
            )
            await session.commit()

        hit = await attributor.attribute(address)
        assert hit, "the harvest never reached the running attributor"
        assert hit[0].entity == "Freshly Harvested Exchange"

    async def test_an_unchanged_store_is_not_rebuilt(
        self, sessions: async_sessionmaker[AsyncSession]
    ) -> None:
        """The watermark exists so the common case stays cheap.

        Re-reading 75,000 rows per lookup would trade a staleness bug for a load
        bug, so a check that finds nothing new must not rebuild the index.
        """
        attributor = await build_store_attributor(sessions, check_interval=0.0)
        before = attributor._inner
        await attributor.attribute(Address(chain="ethereum", value="0x" + "0" * 40))
        assert attributor._inner is before, "rebuilt despite no new label events"

    async def test_an_empty_store_does_not_fall_back_to_the_labelpack_files(
        self, sessions: async_sessionmaker[AsyncSession]
    ) -> None:
        """Mutation testing proved this unpinned too: a quiet re-read of
        labels/ when the store is empty passed every test. Probe with an
        address KNOWN to be in the shipped packs — it must not attribute."""
        packs_dir = Path(__file__).resolve().parents[3] / "labels"
        pack = json.loads((packs_dir / "verified-vasps.json").read_text())
        shipped = pack["labels"][0]
        attributor = await build_store_attributor(sessions)
        hit = await attributor.attribute(Address(chain=shipped["chain"], value=shipped["address"]))
        assert hit == (), "an empty store must mean NO naming, not file fallback"
        # Exactly two sources: sanctions and the (empty) store. A third name
        # here means something snuck a file pack back in.
        assert len(attributor.source_names) == 2
        assert "label-store" in attributor.source_names


class TestFileStoreEquivalence:
    async def test_labels_attribute_identically_from_files_and_from_the_store(
        self, sessions: async_sessionmaker[AsyncSession], tmp_path: Path
    ) -> None:
        """The same pack, loaded the old way (files at startup) and the new
        way (ingested into the lifecycle, read back as active rows), must
        produce byte-identical attribution results — entity, category,
        source, confidence, role, everything a report would cite."""
        (tmp_path / "pack.json").write_text(json.dumps(PACK))
        (file_source,) = load_labelpack_dir(tmp_path)
        old = LabelStoreAttributor([file_source])

        async with sessions() as session:
            service = IntelService(session)
            for row in PACK["labels"]:  # type: ignore[union-attr]
                await service.ingest(
                    IntelClaim(
                        chain=row["chain"],  # type: ignore[index]
                        address=row["address"],  # type: ignore[index]
                        entity=row["entity"],  # type: ignore[index]
                        category=row["category"],  # type: ignore[index]
                        role=row["role"],  # type: ignore[index]
                        confidence=row["confidence"],  # type: ignore[index]
                        method="signature",
                        source=str(PACK["source"]),
                        retrieved_at=T0,
                        source_date=datetime(2026, 8, 10, tzinfo=UTC),
                    )
                )
            await session.commit()
            new = LabelStoreAttributor(
                [StoredLabelSource(await LabelRepository(session).active_labels())]
            )

        for row in PACK["labels"]:  # type: ignore[union-attr]
            address = Address(chain=row["chain"], value=row["address"])  # type: ignore[index]
            old_results = await old.attribute(address)
            new_results = await new.attribute(address)
            assert old_results == new_results, f"attribution diverged for {address}"
            assert old_results, "the fixture must actually attribute something"

    async def test_the_store_lookup_survives_case_like_the_file_path_does(
        self, sessions: async_sessionmaker[AsyncSession]
    ) -> None:
        """Files normalize on load; ingest normalizes on write. A mixed-case
        EVM query must hit either way, and a Tron address must NOT be folded."""
        async with sessions() as session:
            await IntelService(session).ingest(
                IntelClaim(
                    chain="ethereum",
                    address="0xABCD000000000000000000000000000000000001",
                    entity="Acme",
                    category="vasp",
                    role="unknown",
                    confidence=0.9,
                    method="signature",
                    source="acme-por",
                    retrieved_at=T0,
                )
            )
            await session.commit()
            attributor = LabelStoreAttributor(
                [StoredLabelSource(await LabelRepository(session).active_labels())]
            )
        hit = await attributor.attribute(
            Address(chain="ethereum", value="0xAbCd000000000000000000000000000000000001")
        )
        assert hit and hit[0].entity == "Acme"
