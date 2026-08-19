"""The vendored OFAC snapshot loads as dated, sourced claims."""

from cipherchain.analysis.attribution.store import LabelStoreAttributor
from cipherchain.analysis.sanctions import OFAC_SNAPSHOT_DATE, OfacSanctionsSource
from cipherchain.core.models import Address
from cipherchain.investigation.attribution import CATEGORY_SANCTIONED


def test_snapshot_covers_v1_chains() -> None:
    records = list(OfacSanctionsSource().records())
    assert len(records) > 500
    chains = {r.chain for r in records}
    assert {"bitcoin", "ethereum", "tron", "solana"} <= chains
    assert all(r.category == CATEGORY_SANCTIONED for r in records)


def test_every_record_is_a_dated_sourced_claim() -> None:
    for record in OfacSanctionsSource().records():
        assert record.source.startswith("ofac-sdn")
        assert record.source_date == OFAC_SNAPSHOT_DATE
        assert 0.0 < record.confidence < 1.0  # never presented as certainty


def test_addresses_are_deduplicated_across_asset_lists() -> None:
    keys = [r.key for r in OfacSanctionsSource().records()]
    assert len(keys) == len(set(keys))


async def test_known_listed_address_is_found_case_insensitively() -> None:
    source = OfacSanctionsSource()
    eth_record = next(r for r in source.records() if r.chain == "ethereum")
    attributor = LabelStoreAttributor([source])

    # the engine hands adapters' canonical (lowercase) form; the stored
    # dataset uses EIP-55 mixed case — the lookup must bridge the two
    results = await attributor.attribute(Address("ethereum", eth_record.address.lower()))
    assert results and results[0].category == CATEGORY_SANCTIONED
    assert results[0].source_date == OFAC_SNAPSHOT_DATE


async def test_unlisted_address_returns_nothing() -> None:
    attributor = LabelStoreAttributor([OfacSanctionsSource()])
    assert await attributor.attribute(Address("ethereum", "0x" + "9" * 40)) == ()
