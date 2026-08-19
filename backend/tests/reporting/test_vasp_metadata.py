"""Reference data that may not exist, reached without ever breaking a report.

The metadata module is owned by the intel package and may be absent from a
build. Every failure here has to land on the same outcome — "not on file" — or a
missing footnote takes a whole document with it.
"""

from __future__ import annotations

import sys
import types
from datetime import date

import pytest

from cipherchain.investigation.answers import RankedFinding
from cipherchain.reporting.html import render_html
from cipherchain.reporting.vasp import (
    METADATA_MODULE,
    coerce_profile,
    default_vasp_lookup,
    resolve_profile,
    resolve_profiles,
)
from tests.reporting.conftest import make_report, vasp_finding

FAKE_MODULE = "cipherchain.intel._reporting_test_metadata"

ROW = {
    "entity": "Binance",
    "jurisdiction": "Cayman Islands",
    "legal_entity": "Binance Holdings Ltd",
    "kyc_regime": "full KYC",
    "kyc_since": date(2019, 1, 1),
    "le_request_channel": "le.binance.com",
    "source": "public filings",
    "source_date": date(2026, 3, 1),
}


def _install(monkeypatch: pytest.MonkeyPatch, **attributes: object) -> None:
    """Stand a fake metadata module up where the probe will look for it."""
    module = types.ModuleType(FAKE_MODULE)
    for name, value in attributes.items():
        setattr(module, name, value)
    monkeypatch.setitem(sys.modules, FAKE_MODULE, module)
    monkeypatch.setattr("cipherchain.reporting.vasp.METADATA_MODULE", FAKE_MODULE)


def test_a_missing_metadata_module_is_absence_and_not_an_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A build without the intel table still produces the whole report."""
    monkeypatch.setattr(
        "cipherchain.reporting.vasp.METADATA_MODULE", "cipherchain.intel._absent_module"
    )
    assert default_vasp_lookup(session=None) is None


def test_a_module_exposing_nothing_recognisable_is_absence_too(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The probe guesses; guessing wrong must cost nothing but the metadata."""
    _install(monkeypatch, something_else=lambda: None)
    assert default_vasp_lookup(session=None) is None


async def test_a_module_level_lookup_is_found_and_used(monkeypatch: pytest.MonkeyPatch) -> None:
    def report_lookup(*, chain: str, address: str, entity: str | None) -> dict[str, object]:
        assert (chain, address, entity) == ("testchain", "0xbinance", "Binance")
        return ROW

    _install(monkeypatch, report_lookup=report_lookup)
    lookup = default_vasp_lookup(session=None)
    assert lookup is not None
    profile = await resolve_profile(
        lookup, chain="testchain", address="0xbinance", entity="Binance"
    )
    assert profile is not None
    assert profile.jurisdiction == "Cayman Islands"
    assert profile.le_request_channel == "le.binance.com"


async def test_an_async_lookup_is_awaited(monkeypatch: pytest.MonkeyPatch) -> None:
    """The real implementation reads a table, so the result is a coroutine."""

    async def lookup(*, chain: str, address: str, entity: str | None) -> dict[str, object]:
        return ROW

    profile = await resolve_profile(lookup, chain="c", address="a", entity="Binance")
    assert profile is not None and profile.legal_entity == "Binance Holdings Ltd"


async def test_a_repository_class_is_asked_by_entity(monkeypatch: pytest.MonkeyPatch) -> None:
    """Matches the repository convention the storage layer already uses."""

    class VaspMetadataRepository:
        def __init__(self, session: object) -> None:
            self.session = session

        async def for_entity(self, entity: str) -> dict[str, object] | None:
            return ROW if entity == "Binance" else None

    _install(monkeypatch, VaspMetadataRepository=VaspMetadataRepository)
    lookup = default_vasp_lookup(session=object())
    assert lookup is not None
    found = await resolve_profile(lookup, chain="c", address="a", entity="Binance")
    missing = await resolve_profile(lookup, chain="c", address="a", entity="Nobody")
    assert found is not None and found.entity == "Binance"
    assert missing is None


async def test_a_lookup_that_raises_costs_a_footnote_and_not_the_document() -> None:
    """Reference data is not worth an unrenderable report."""

    def exploding(*, chain: str, address: str, entity: str | None) -> object:
        raise RuntimeError("metadata table is on fire")

    assert await resolve_profile(exploding, chain="c", address="a", entity="Binance") is None

    report = make_report(
        ranked=[RankedFinding(vasp_finding("0xbinance", named=True), hop=2)],
        profiles={},
    )
    assert "Coverage and caveats" in render_html(report)


async def test_a_missing_row_is_simply_absent() -> None:
    async def empty(*, chain: str, address: str, entity: str | None) -> None:
        return None

    assert await resolve_profile(empty, chain="c", address="a", entity="Binance") is None
    assert await resolve_profile(None, chain="c", address="a", entity="Binance") is None


def test_a_row_of_blanks_is_reported_as_absent_rather_than_as_a_table() -> None:
    """Empty cells under 'jurisdiction' read as 'there isn't one', which is false."""
    assert coerce_profile({"entity": "Binance"}) is None
    assert coerce_profile({"entity": "Binance", "jurisdiction": "  "}) is None
    assert coerce_profile(None, entity="Binance") is None


def test_a_row_is_read_off_an_object_as_readily_as_a_mapping() -> None:
    """The schema contract is frozen; the class carrying it is not ours to fix."""

    class Row:
        entity = "Kraken"
        jurisdiction = "United States"
        le_request_channel = "compliance@kraken.com"

    profile = coerce_profile(Row())
    assert profile is not None
    assert profile.entity == "Kraken"
    assert profile.jurisdiction == "United States"
    # Fields the row does not carry are absent, not invented.
    assert profile.legal_entity is None


def test_the_real_module_name_is_the_one_the_intel_package_owns() -> None:
    """Guards a rename of the constant away from the agreed module path."""
    assert METADATA_MODULE == "cipherchain.intel.vasp_metadata"


async def test_an_endpoint_no_claim_named_is_never_even_looked_up() -> None:
    """Metadata describes an operator a third-party claim named — nobody else.

    Profiles are keyed by address, so an address-indexed lookup will answer for
    an endpoint resting on nothing but a behavioural inference. Asking it at all
    is the mistake: what comes back is a legal entity, a jurisdiction and a
    filing channel, which is "this address behaves like a custodian" laundered
    into "serve Binance Holdings Ltd in the Cayman Islands".
    """
    asked: list[str] = []

    async def by_address(*, chain: str, address: str, entity: str | None) -> dict[str, object]:
        asked.append(address)
        return {**ROW, "entity": entity or address}

    profiles = await resolve_profiles(
        by_address, [("testchain", "0xa", "Binance"), ("testchain", "0xb", None)]
    )
    assert asked == ["0xa"], "the unnamed endpoint was offered to the lookup"
    assert set(profiles) == {"0xa"}
