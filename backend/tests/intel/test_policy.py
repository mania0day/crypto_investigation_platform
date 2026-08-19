"""The lifecycle policy, rule by rule (LABEL_INTELLIGENCE.md §4).

Corroboration errors are asymmetric — a false NO waits, a false YES puts an
unverified claim into the attributor — so most of these tests pin refusals.
"""

from datetime import UTC, datetime

import pytest

from cipherchain.intel.policy import IntelClaim, arrival_status, corroborates, entity_stem
from cipherchain.storage.repositories import StoredLabel

T0 = datetime(2026, 8, 11, tzinfo=UTC)


def stored(
    *,
    entity: str,
    source: str,
    status: str = "active",
    category: str = "vasp",
    label_id: int = 1,
    address: str = "0xaaa",
    method: str | None = None,
) -> StoredLabel:
    return StoredLabel(
        id=label_id,
        chain="ethereum",
        address=address,
        entity=entity,
        category=category,
        role="unknown",
        confidence=0.75,
        status=status,
        method=method or ("community" if status == "pending" else "licensed_dataset"),
        source=source,
        source_date=None,
        retrieved_at=T0,
        corroborated_by=None,
        evidence_url=None,
        reporter=None,
    )


class TestArrival:
    def test_every_trusted_method_activates_on_arrival(self) -> None:
        """Ruling 2 restricts sources so that what survives the bar may
        attribute — the vetting is the source restriction itself."""
        for method in ("signature", "first_party_published", "licensed_dataset"):
            assert arrival_status(method) == "active"

    def test_community_arrives_pending_and_so_does_anything_unknown(self) -> None:
        assert arrival_status("community") == "pending"
        # An unrecognized method must never default to trust.
        assert arrival_status("carrier_pigeon") == "pending"


class TestEntityStem:
    def test_wallet_index_and_role_suffix_are_our_annotations(self) -> None:
        assert entity_stem("Binance 14") == "binance"
        assert entity_stem("Binance (operational address)") == "binance"
        assert entity_stem("OKX 213") == "okx"
        assert entity_stem("binance") == "binance"

    def test_a_different_stem_is_a_different_operator(self) -> None:
        assert entity_stem("Binance Charity") != entity_stem("Binance")
        assert entity_stem("Gate.io") != entity_stem("Gate")

    def test_a_version_is_a_discriminator_not_an_index(self) -> None:
        """Review found the looser strip collapsed V2 and V3 into one stem:
        a digit is annotation only when a separator precedes it."""
        assert entity_stem("Aave V2") != entity_stem("Aave V3")
        assert entity_stem("Aave V2") == "aave v2"


class TestCorroboration:
    def test_an_independent_active_source_with_the_same_stem_promotes(self) -> None:
        pending = stored(entity="Binance", source="community", status="pending")
        active = stored(entity="Binance 14", source="etherscan-tags")
        assert corroborates(pending, active) is True

    def test_pending_cannot_corroborate_pending(self) -> None:
        """Two unverified claims agreeing is agreement, not verification."""
        pending = stored(entity="Binance", source="community", status="pending")
        other = stored(entity="Binance", source="other-report", status="pending")
        assert corroborates(pending, other) is False

    def test_a_source_cannot_corroborate_itself(self) -> None:
        pending = stored(entity="Binance", source="community", status="pending")
        same = stored(entity="Binance 14", source="community")
        assert corroborates(pending, same) is False

    def test_a_different_category_contradicts_rather_than_confirms(self) -> None:
        pending = stored(entity="Binance", source="community", status="pending")
        sanction = stored(entity="Binance", source="ofac", category="sanctioned")
        assert corroborates(pending, sanction) is False

    def test_a_different_stem_waits_in_both_directions(self) -> None:
        """Both directions, because mutation testing proved one alone cannot
        kill a prefix-match rewrite: 'Binance Charity'.startswith('Binance')
        is True, and the mirrored assertion is what catches it."""
        pending = stored(entity="Binance", source="community", status="pending")
        charity = stored(entity="Binance Charity", source="etherscan-tags")
        assert corroborates(pending, charity) is False

        pending_charity = stored(entity="Binance Charity", source="community", status="pending")
        binance = stored(entity="Binance", source="etherscan-tags")
        assert corroborates(pending_charity, binance) is False

    def test_an_empty_stem_never_corroborates_either_way(self) -> None:
        """An entity that is ALL annotation ('14', '(deposit)') names nobody,
        and nobody cannot confirm or be confirmed — an empty-stem candidate
        under a prefix rewrite would vacuously corroborate everything."""
        pending = stored(entity="14", source="community", status="pending")
        active = stored(entity="14", source="etherscan-tags")
        assert corroborates(pending, active) is False

        named = stored(entity="Binance", source="community", status="pending")
        unnamed = stored(entity="14", source="etherscan-tags")
        assert corroborates(named, unnamed) is False

    def test_only_a_trusted_method_may_corroborate(self) -> None:
        """A promoted community report is active, but activation is not
        trust: report→report chains would let two sock-puppets hold each
        other active after their real basis retired."""
        pending = stored(entity="Binance", source="report:key-b", status="pending")
        promoted_report = stored(
            entity="Binance 14", source="report:key-a", status="active", method="community"
        )
        assert corroborates(pending, promoted_report) is False

    def test_evidence_is_bound_to_its_address(self) -> None:
        """Mutation testing proved the caller's join could silently vanish;
        the predicate now holds its own ground."""
        pending = stored(entity="Binance", source="community", status="pending", address="0xbbb")
        elsewhere = stored(entity="Binance 14", source="etherscan-tags", address="0xaaa")
        assert corroborates(pending, elsewhere) is False


class TestUntrustedEntityConstraints:
    """Stem matching ignores parentheticals and trailing indexes — annotation
    syntax in OUR packs, a smuggling channel in a report. Review demonstrated
    'Binance (successor wallet 0xATTACKER)' stemming to 'binance', promoting
    against real Binance data, and surfacing verbatim as a citable label."""

    def build(self, entity: str, method: str = "community") -> IntelClaim:
        return IntelClaim(
            chain="ethereum",
            address="0xaaa",
            entity=entity,
            category="vasp",
            role="unknown",
            confidence=0.4,
            method=method,
            source="report:key-1",
            retrieved_at=T0,
        )

    def test_the_parenthetical_smuggling_channel_is_closed(self) -> None:
        with pytest.raises(ValueError, match="annotation syntax"):
            self.build("Binance (successor wallet 0xATTACKER)")

    def test_prose_urls_and_multiline_are_rejected(self) -> None:
        with pytest.raises(ValueError, match="URL"):
            self.build("Binance recovery via https://scam.example")
        with pytest.raises(ValueError, match="single line"):
            self.build("Binance\ncontact support")
        with pytest.raises(ValueError, match="too long"):
            self.build("B" * 65)
        with pytest.raises(ValueError, match="empty"):
            self.build("   ")

    def test_a_plain_name_passes_and_curated_packs_keep_their_syntax(self) -> None:
        assert self.build("Binance").entity == "Binance"
        # Trusted-method claims come from OUR packs, where the parenthetical
        # IS the role annotation ("Binance (operational address)").
        trusted = self.build("Binance (operational address)", method="signature")
        assert trusted.entity == "Binance (operational address)"
