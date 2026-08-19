"""Per-operator filing facts, and the join that reaches them from a label.

REACHING_THE_VASP.md §6.2: a report naming "Binance" does not tell an officer
which Binance. What is locked down here is the join key (the label's entity
value), the refusal to invent an unknown descriptive field, and the mandatory
provenance that makes the record weighable at all.
"""

from datetime import UTC, date, datetime

import pytest
from sqlalchemy import text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from cipherchain.storage.repositories import LabelRepository, VaspMetadataRepository

# The label harvest keeps an instant; a document keeps a day.
HARVESTED = datetime(2026, 8, 16, 12, 0, tzinfo=UTC)
D0 = date(2026, 8, 16)
D1 = date(2026, 8, 17)


class TestReadingByEntity:
    async def test_metadata_is_reached_from_the_entity_a_label_names(
        self, session: AsyncSession
    ) -> None:
        """The join key is `labels.entity` — a plain string on both sides, since
        a label's identity is (chain, address, source) and one operator recurs
        in as many label rows as it has addresses."""
        labels = LabelRepository(session)
        await labels.upsert_claim(
            chain="ethereum",
            address="0xaaa",
            entity="Acme Exchange",
            category="vasp",
            role="operational",
            confidence=0.9,
            status="active",
            method="signature",
            source="acme-por",
            retrieved_at=HARVESTED,
        )
        vasps = VaspMetadataRepository(session)
        await vasps.upsert(
            entity="Acme Exchange",
            jurisdiction="MT",
            legal_entity="Acme (Services) Holdings Ltd",
            kyc_regime="full KYC",
            kyc_since=date(2021, 8, 1),
            le_request_channel="https://acme.example/le",
            source="acme-terms-page",
            source_date=D0,
        )
        (claim,) = await labels.claims_for("ethereum", "0xaaa")
        metadata = await vasps.for_entity(claim.entity)
        assert metadata is not None
        assert metadata.legal_entity == "Acme (Services) Holdings Ltd"
        assert metadata.jurisdiction == "MT"
        assert metadata.kyc_since == date(2021, 8, 1)

    async def test_an_entity_we_hold_nothing_about_reads_as_nothing(
        self, session: AsyncSession
    ) -> None:
        """None, not an empty record: "we hold no metadata for this operator" is
        a coverage gap the report must state, and it must not render the same as
        metadata that happens to say little."""
        vasps = VaspMetadataRepository(session)
        assert await vasps.for_entity("Unknown Exchange") is None


class TestRecording:
    async def test_re_recording_an_entity_corrects_the_row_rather_than_duplicating_it(
        self, session: AsyncSession
    ) -> None:
        """One operator has one set of filing facts. A second row would be a
        contradiction, not the corroboration a second LABEL row represents."""
        vasps = VaspMetadataRepository(session)
        first = await vasps.upsert(
            entity="Acme Exchange", jurisdiction="MT", source="terms-page", source_date=D0
        )
        second = await vasps.upsert(
            entity="Acme Exchange", jurisdiction="IE", source="licence-register", source_date=D1
        )
        assert first == second
        metadata = await vasps.for_entity("Acme Exchange")
        assert metadata is not None
        assert (metadata.jurisdiction, metadata.source) == ("IE", "licence-register")

    async def test_a_field_the_source_no_longer_states_is_cleared(
        self, session: AsyncSession
    ) -> None:
        """Refresh REPLACES, nulls included. Merging the new record over the old
        would let a request channel that no longer exists survive forever
        because nothing ever restated it — and an officer would file into it."""
        vasps = VaspMetadataRepository(session)
        await vasps.upsert(
            entity="Acme Exchange",
            le_request_channel="https://acme.example/le-old",
            source="terms-page",
            source_date=D0,
        )
        await vasps.upsert(entity="Acme Exchange", source="terms-page", source_date=D1)
        metadata = await vasps.for_entity("Acme Exchange")
        assert metadata is not None
        assert metadata.le_request_channel is None

    async def test_metadata_may_be_partial(self, session: AsyncSession) -> None:
        """An operator we know only the name and jurisdiction of is recordable.
        A NOT NULL on the descriptive fields would have pushed someone into
        guessing a jurisdiction, and a guessed jurisdiction sends the subpoena
        to the wrong regulator."""
        vasps = VaspMetadataRepository(session)
        await vasps.upsert(
            entity="Sparse Exchange", jurisdiction="SG", source="licence-register", source_date=D0
        )
        metadata = await vasps.for_entity("Sparse Exchange")
        assert metadata is not None
        assert (metadata.legal_entity, metadata.kyc_regime, metadata.kyc_since) == (
            None,
            None,
            None,
        )

    async def test_metadata_is_never_unsourced(self, session: AsyncSession) -> None:
        """This record carries the evidentiary weight of a label, so it obeys
        the same rule: sourced, dated, never invented. Raw SQL, because the
        repository signature already makes provenance mandatory and the DB has
        to hold the line under anything else that writes here."""
        with pytest.raises(IntegrityError):
            await session.execute(
                text("INSERT INTO vasp_metadata (entity, source_date) VALUES ('X', CURRENT_DATE)")
            )
        await session.rollback()
        with pytest.raises(IntegrityError):
            await session.execute(
                text("INSERT INTO vasp_metadata (entity, source) VALUES ('X', 'somewhere')")
            )
