"""What makes a VASP answer filable — and what must never be invented.

The output of this system is served on a company. Two failures are fatal and
both are silent, so both are pinned here: metadata attached to the wrong
operator (a subpoena to the wrong country), and a fabricated value standing
in for a fact nobody established (a subpoena nobody can defend). Everything
below exists to keep null meaning "not established" and lookup meaning
"this exact operator".
"""

from __future__ import annotations

import importlib.util
import json
from collections import Counter
from datetime import date
from pathlib import Path
from typing import Any

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from cipherchain.core.errors import ConfigurationError
from cipherchain.intel.policy import entity_stem
from cipherchain.intel.vasp_metadata import (
    DEFAULT_METADATA_PATH,
    VaspMetadata,
    load_vasp_metadata,
    metadata_for,
    report_lookup,
)

BACKEND_DIR = Path(__file__).resolve().parents[2]
LABELS_DIR = BACKEND_DIR.parent / "labels"
SCRIPT = BACKEND_DIR / "scripts" / "import_vasp_metadata.py"

ROW: dict[str, Any] = {
    "entity": "Acme Exchange",
    "jurisdiction": "Ruritania",
    "legal_entity": "Acme Exchange Ltd.",
    "kyc_regime": "Ruritanian AML regime",
    "kyc_since": "2020-01-02",
    "le_request_channel": "le@acme.example",
    "source": "Ruritanian company register",
    "notes": None,
}


def write_file(path: Path, rows: list[dict[str, Any]], **top: Any) -> Path:
    payload: dict[str, Any] = {"source_date": "2026-08-16", "vasps": rows, **top}
    path.write_text(json.dumps(payload))
    return path


@pytest.fixture(scope="module")
def script() -> Any:
    spec = importlib.util.spec_from_file_location("import_vasp_metadata", SCRIPT)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture(scope="module")
def shipped_label_entities() -> Counter[str]:
    """Every entity string the shipped packs actually carry, by count.

    Read once: these files hold ~75k labels.
    """
    counts: Counter[str] = Counter()
    for path in sorted(LABELS_DIR.glob("*.json")):
        pack = json.loads(path.read_text())
        for label in pack.get("labels", []):
            if label.get("category") == "vasp":
                counts[str(label["entity"])] += 1
    return counts


class TestLookup:
    def test_a_known_entity_returns_its_filing_facts(self) -> None:
        found = metadata_for("Binance")
        assert found is not None
        assert found.legal_entity == "Binance Holdings Limited"
        assert found.jurisdiction == "Cayman Islands"
        assert found.kyc_since == date(2021, 8, 20)
        assert found.is_serviceable

    def test_an_annotated_label_entity_still_finds_its_operator(self) -> None:
        """The strings that reach a caller are label strings, and ours carry
        role annotations. If the join used them verbatim, the 5,021 'Binance
        (deposit address)' labels — the ones a trace actually lands on —
        would all resolve to nothing.
        """
        for name in (
            "Binance (deposit address)",
            "Binance (operational address)",
            "binance",
            "Binance 14",
        ):
            found = metadata_for(name)
            assert found is not None, name
            assert found.entity == "Binance"

    def test_an_unknown_entity_is_none_not_an_exception(self) -> None:
        """Absence is a first-class answer: most operators on earth are not in
        this file, and reaching one is an ordinary trace outcome, not an
        error condition.
        """
        assert metadata_for("Some Exchange We Have Never Heard Of") is None

    def test_an_entity_that_names_nobody_matches_nothing(self) -> None:
        """An empty stem must not become a wildcard key. '14' and '(deposit)'
        are pure annotation; if the index could hold an empty stem, every
        unnamed entity would collide onto whichever row got there first.
        """
        for name in ("", "   ", "14", "(deposit address)"):
            assert metadata_for(name) is None, name

    def test_a_null_field_stays_null_and_is_never_defaulted(self) -> None:
        """Bitget is the largest labelled operator in the packs and the one we
        can least identify. The honest answer is null — a placeholder
        jurisdiction here is a subpoena to the wrong country.
        """
        found = metadata_for("Bitget (deposit address)")
        assert found is not None
        assert found.jurisdiction is None
        assert found.legal_entity is None
        assert found.le_request_channel is None
        assert not found.is_serviceable
        assert found.notes  # and it says so
        assert found.source_date == date(2026, 8, 16)

    def test_an_index_is_injectable_so_lookups_are_not_process_state(
        self, tmp_path: Path
    ) -> None:
        index = load_vasp_metadata(write_file(tmp_path / "m.json", [ROW]))
        assert index.lookup("Acme Exchange (deposit address)") == index.entries[0]
        assert index.lookup("Binance") is None
        assert len(index) == 1
        assert list(index) == list(index.entries)


class TestLoaderRefusals:
    def test_two_rows_for_one_operator_are_refused(self, tmp_path: Path) -> None:
        """Order is not a decision. The second row would silently replace the
        first in the index and its jurisdiction would simply disappear — the
        same failure the labelpack collision guard exists for.
        """
        twin = {**ROW, "entity": "Acme Exchange (custody)", "jurisdiction": "Elsewhere"}
        with pytest.raises(ConfigurationError, match="same operator"):
            load_vasp_metadata(write_file(tmp_path / "m.json", [ROW, twin]))

    def test_an_omitted_field_is_refused_rather_than_read_as_null(
        self, tmp_path: Path
    ) -> None:
        """Omission and null read identically once loaded, and they are not
        the same act: null is a decision, omission is a field nobody
        considered. This also turns a misspelled key into a load error
        instead of a silently absent jurisdiction.
        """
        partial = {key: value for key, value in ROW.items() if key != "jurisdiction"}
        with pytest.raises(ConfigurationError, match="omits jurisdiction"):
            load_vasp_metadata(write_file(tmp_path / "m.json", [partial]))

    def test_a_misspelled_field_is_refused(self, tmp_path: Path) -> None:
        typo = {**ROW, "jurisdication": "Ruritania"}
        with pytest.raises(ConfigurationError, match="unknown field"):
            load_vasp_metadata(write_file(tmp_path / "m.json", [typo]))

    def test_an_unexplained_null_forum_is_refused(self, tmp_path: Path) -> None:
        """The file's contract is 'where we are not confident, say so'. An
        unexplained null cannot be told apart from an unfinished row, and the
        reader of a filing has no way to ask.
        """
        silent = {**ROW, "jurisdiction": None, "notes": None}
        with pytest.raises(ConfigurationError, match="must SAY so"):
            load_vasp_metadata(write_file(tmp_path / "m.json", [silent]))
        explained = {**ROW, "jurisdiction": None, "notes": "no registry check was made"}
        index = load_vasp_metadata(write_file(tmp_path / "ok.json", [explained]))
        assert index.entries[0].jurisdiction is None

    def test_an_empty_string_is_not_an_acceptable_answer(self, tmp_path: Path) -> None:
        """'' is a null in disguise, and every truthiness check downstream
        would read it as an answer.
        """
        with pytest.raises(ConfigurationError, match="non-empty string or null"):
            load_vasp_metadata(write_file(tmp_path / "m.json", [{**ROW, "jurisdiction": ""}]))

    def test_a_row_without_provenance_is_refused(self, tmp_path: Path) -> None:
        with pytest.raises(ConfigurationError, match="provenance is required"):
            load_vasp_metadata(write_file(tmp_path / "m.json", [{**ROW, "source": None}]))

    def test_an_undated_row_is_refused_when_the_file_dates_nothing_either(
        self, tmp_path: Path
    ) -> None:
        """Corporate facts move. A row nobody can date cannot be weighed for
        staleness, which is the only defence against a two-year-old
        jurisdiction going into a filing.
        """
        path = tmp_path / "m.json"
        path.write_text(json.dumps({"vasps": [ROW]}))
        with pytest.raises(ConfigurationError, match="no source_date"):
            load_vasp_metadata(path)

    def test_a_malformed_date_is_refused(self, tmp_path: Path) -> None:
        with pytest.raises(ConfigurationError, match="is not a date"):
            load_vasp_metadata(write_file(tmp_path / "m.json", [{**ROW, "kyc_since": "August"}]))

    def test_an_unreadable_file_raises_rather_than_loading_nothing(
        self, tmp_path: Path
    ) -> None:
        """Half-loading is worse than failing: the rows that dropped out look
        exactly like honest nulls.
        """
        with pytest.raises(ConfigurationError, match="cannot read"):
            load_vasp_metadata(tmp_path / "absent.json")
        broken = tmp_path / "broken.json"
        broken.write_text("{not json")
        with pytest.raises(ConfigurationError, match="cannot read"):
            load_vasp_metadata(broken)


class TestShippedFile:
    def test_the_shipped_file_lives_outside_the_labelpack_glob(self) -> None:
        """labels/*.json is globbed by the labelpack loader and by
        scripts/import_labelpacks.py, both of which refuse a file that is not
        a labelpack. Moving this file up one directory breaks the label
        import and its regression test.
        """
        assert DEFAULT_METADATA_PATH.is_file()
        assert DEFAULT_METADATA_PATH.parent.name == "metadata"
        assert DEFAULT_METADATA_PATH not in set(LABELS_DIR.glob("*.json"))

    def test_every_row_matches_a_real_label_entity(
        self, shipped_label_entities: Counter[str]
    ) -> None:
        """A metadata row that no label can reach is dead weight: it inflates
        the file's apparent coverage while answering nothing.
        """
        label_stems = {entity_stem(name) for name in shipped_label_entities}
        orphans = [row.entity for row in load_vasp_metadata() if row.stem not in label_stems]
        assert orphans == []

    def test_the_operators_the_packs_actually_name_are_all_covered(
        self, shipped_label_entities: Counter[str]
    ) -> None:
        """The join must hold for the whole shipped VASP label set, not for a
        sample of it: an uncovered operator is a trace that ends in a name
        nobody can file against.
        """
        index = load_vasp_metadata()
        uncovered = sorted(
            {name for name in shipped_label_entities if index.lookup(name) is None}
        )
        assert uncovered == []
        assert len(index) >= 15

    def test_no_asserted_fact_is_left_without_a_source(self) -> None:
        """Provenance is mandatory even on the rows that assert nothing — "we
        could not establish this" is itself a claim a reader must be able to
        date, and a row dated in the future was never checked at all.
        """
        for row in load_vasp_metadata():
            assert row.source
            assert row.source_date <= date.today()
            if not row.is_serviceable:
                assert row.notes, f"{row.entity} is unserviceable and does not say why"


class TestReportingProtocol:
    """reporting.vasp probes this module by NAME and calls what it finds with
    (chain, address, entity) keywords, swallowing any exception into "not on
    file". A signature mismatch would therefore blank the metadata section of
    every report and log a warning nobody reads — the one integration in this
    module that fails silently, so it is pinned from this side too.
    """

    def test_the_reporting_probe_binds_this_module_and_gets_an_answer(self) -> None:
        module = importlib.import_module("cipherchain.reporting.vasp")
        bound = module.default_vasp_lookup()
        assert bound is not None
        profile = bound(chain="ethereum", address="0xabc", entity="Binance (deposit address)")
        assert profile is not None
        assert profile.legal_entity == "Binance Holdings Limited"

    def test_the_lookup_answers_by_operator_and_ignores_the_address(self) -> None:
        """One operator's filing facts are identical for every address it
        controls, so the address key is accepted and not consulted.
        """
        first = report_lookup(chain="ethereum", address="0xaaa", entity="Kraken")
        second = report_lookup(chain="bitcoin", address="bc1qzzz", entity="Kraken")
        assert first is not None and first == second

    def test_an_endpoint_with_no_name_gets_no_metadata(self) -> None:
        """An unnamed endpoint has no operator to describe. Returning anything
        here would attach filing facts to an address nothing has named — the
        heuristic-into-attribution blur the taxonomy forbids.
        """
        assert report_lookup(chain="ethereum", address="0xaaa", entity=None) is None


class TestImportScript:
    async def test_a_second_import_of_the_same_file_writes_nothing(
        self, script: Any, session: AsyncSession
    ) -> None:
        """Idempotence is not tidiness here. This is the 'who do we serve'
        record; a re-import that rewrites every row destroys any signal about
        when a fact was first established.
        """
        index = load_vasp_metadata()
        first = await script.import_index(session, index)
        await session.commit()
        assert first["added"] == len(index)
        assert first["updated"] == 0 and first["unchanged"] == 0

        second = await script.import_index(session, index)
        await session.commit()
        assert second["unchanged"] == len(index)
        assert second["added"] == 0 and second["updated"] == 0

        total = await session.scalar(text("SELECT count(*) FROM vasp_metadata"))
        assert total == len(index)

    async def test_the_table_is_keyed_by_stem_so_a_sql_join_resolves(
        self, script: Any, session: AsyncSession
    ) -> None:
        """The table's contract says it stores the stem and callers join on
        the stem. Storing the display name instead would leave every SQL
        consumer joining "Binance" against "binance" and finding nothing.
        """
        await script.import_index(session, load_vasp_metadata())
        await session.commit()
        stored = set(
            (await session.scalars(text("SELECT entity FROM vasp_metadata"))).all()
        )
        assert "binance" in stored
        assert "Binance" not in stored
        assert all(entity == entity_stem(entity) for entity in stored)

    async def test_a_corrected_fact_updates_only_its_own_row(
        self, script: Any, session: AsyncSession
    ) -> None:
        await script.import_index(session, load_vasp_metadata())
        await session.commit()

        corrected = VaspMetadata(
            entity="Bitget",
            jurisdiction="Seychelles",
            legal_entity="Bitget Limited",
            kyc_regime="verified against the register",
            kyc_since=date(2023, 9, 1),
            le_request_channel="le@example.invalid",
            source="operator registry extract",
            source_date=date(2026, 8, 16),
            notes=None,
        )
        assert await script.upsert_metadata(session, corrected) == "updated"
        assert await script.upsert_metadata(session, corrected) == "unchanged"
        await session.commit()

        row = (
            await session.execute(
                text("SELECT jurisdiction, kyc_since FROM vasp_metadata WHERE entity = :e"),
                {"e": "bitget"},
            )
        ).first()
        assert row is not None
        assert row[0] == "Seychelles"
        assert row[1] == date(2023, 9, 1)

    async def test_nulls_land_as_sql_null_not_as_a_placeholder(
        self, script: Any, session: AsyncSession
    ) -> None:
        """The whole point of the file survives the round trip only if 'not
        established' stays distinguishable from 'established as blank' in the
        table a report will read.
        """
        await script.import_index(session, load_vasp_metadata())
        await session.commit()

        row = (
            await session.execute(
                text(
                    "SELECT jurisdiction, legal_entity, le_request_channel, kyc_since "
                    "FROM vasp_metadata WHERE entity = :e"
                ),
                {"e": "bitget"},
            )
        ).first()
        assert row is not None
        assert list(row) == [None, None, None, None]

        binance = (
            await session.execute(
                text("SELECT kyc_since, jurisdiction FROM vasp_metadata WHERE entity = :e"),
                {"e": "binance"},
            )
        ).first()
        assert binance is not None
        assert binance[0] == date(2021, 8, 20)
        assert binance[1] == "Cayman Islands"
