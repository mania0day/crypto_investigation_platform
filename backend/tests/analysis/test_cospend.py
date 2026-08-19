"""Co-spend clustering, and the merge it must refuse.

Clustering is transitive, so a single wrong edge does not stay local: it
welds two unrelated clusters into one and every label on either side then
names the wrong party. Most of these tests are about what does NOT get
clustered.
"""

from typing import ClassVar

import pytest

from cipherchain.analysis.clustering import (
    ClusterProposal,
    SpendingTransaction,
    build_clusters,
    looks_like_coinjoin,
    propose_cluster_labels,
)


def tx(hash_: str, inputs: tuple[int, ...], outputs: tuple[int, ...] = ()) -> SpendingTransaction:
    return SpendingTransaction(tx_hash=hash_, input_address_ids=inputs, output_amounts=outputs)


class TestClusterBuilding:
    def test_co_spending_addresses_land_in_one_cluster(self) -> None:
        """Spending both inputs required both keys. That is the whole claim."""
        report = build_clusters([tx("0xa", (1, 2), (500, 300))])
        assert report.clusters == (frozenset({1, 2}),)

    def test_clusters_merge_transitively_across_transactions(self) -> None:
        """A with B, then B with C, means all three share a controller."""
        report = build_clusters([tx("0xa", (1, 2), (500, 300)), tx("0xb", (2, 3), (900, 100))])
        assert report.clusters == (frozenset({1, 2, 3}),)

    def test_single_input_transactions_form_no_cluster(self) -> None:
        """One spender says nothing about anyone else."""
        report = build_clusters([tx("0xa", (1,), (500,)), tx("0xb", (2,), (300,))])
        assert report.clusters == ()
        assert report.clustered_addresses == 0

    def test_repeated_address_in_one_transaction_is_not_a_cluster(self) -> None:
        """Two inputs from the SAME address is one spender, not two."""
        report = build_clusters([tx("0xa", (1, 1, 1), (500,))])
        assert report.clusters == ()

    def test_unrelated_groups_stay_separate(self) -> None:
        report = build_clusters([tx("0xa", (1, 2), (500, 300)), tx("0xb", (8, 9), (700, 200))])
        assert set(report.clusters) == {frozenset({1, 2}), frozenset({8, 9})}


class TestCoinJoinGuard:
    """The failure this module mostly exists to prevent."""

    def test_equal_outputs_with_several_inputs_reads_as_coinjoin(self) -> None:
        """Whirlpool's shape: five participants, five identical payouts."""
        candidate = tx("0xcj", (1, 2, 3, 4, 5), (100, 100, 100, 100, 100))
        assert looks_like_coinjoin(candidate) is True

    def test_a_coinjoin_never_merges_its_participants(self) -> None:
        """The catastrophic merge: five strangers into one 'entity'."""
        report = build_clusters([tx("0xcj", (1, 2, 3, 4, 5), (100, 100, 100, 100, 100))])
        assert report.clusters == ()
        assert report.coinjoins_skipped == 1

    def test_a_coinjoin_cannot_bridge_two_real_clusters(self) -> None:
        """Transitivity is why this matters. Without the guard, Alice's
        cluster and Bob's cluster become one, and a label on either names
        the other's owner."""
        report = build_clusters(
            [
                tx("0xa", (1, 2), (500, 300)),  # Alice
                tx("0xb", (8, 9), (700, 200)),  # Bob
                tx("0xcj", (2, 8, 30, 31), (100, 100, 100, 100)),  # a CoinJoin joining them
            ]
        )
        assert set(report.clusters) == {frozenset({1, 2}), frozenset({8, 9})}
        assert report.coinjoins_skipped == 1

    def test_two_inputs_paying_equal_amounts_is_an_ordinary_spend(self) -> None:
        """Paying two people the same round amount is common and must still
        cluster — otherwise the guard costs most of the real signal."""
        candidate = tx("0xa", (1, 2), (100, 100))
        assert looks_like_coinjoin(candidate) is False
        assert build_clusters([candidate]).clusters == (frozenset({1, 2}),)

    def test_many_inputs_with_distinct_outputs_still_clusters(self) -> None:
        """A big consolidation is not a CoinJoin: no repeated output value."""
        candidate = tx("0xa", (1, 2, 3, 4), (990, 10))
        assert looks_like_coinjoin(candidate) is False
        assert build_clusters([candidate]).clusters == (frozenset({1, 2, 3, 4}),)

    def test_a_transaction_touching_a_mixer_is_never_clustered(self) -> None:
        """A mixer's inputs belong to its participants, not to the mixer."""
        report = build_clusters([tx("0xa", (1, 77), (500, 300))], mixer_address_ids=frozenset({77}))
        assert report.clusters == ()
        assert report.mixer_transactions_skipped == 1


class TestProposals:
    SEED: ClassVar[dict[int, tuple[str, str, str, float]]] = {
        1: ("Binance", "vasp", "etherscan-tags", 0.75)
    }

    def test_a_seed_label_names_the_rest_of_its_cluster(self) -> None:
        report = build_clusters([tx("0xa", (1, 2, 3), (500, 300))])
        proposals, conflicts = propose_cluster_labels(report, self.SEED)
        assert conflicts == []
        (proposal,) = proposals
        assert proposal.entity == "Binance"
        assert proposal.new_address_ids == frozenset({2, 3})
        assert proposal.seed_address_ids == frozenset({1})

    def test_a_derived_claim_never_outranks_its_seed(self) -> None:
        report = build_clusters([tx("0xa", (1, 2), (500, 300))])
        (proposal,), _ = propose_cluster_labels(report, self.SEED)
        assert proposal.confidence < 0.75

    def test_an_unlabelled_cluster_proposes_nothing(self) -> None:
        """Clustering without a seed produces no name — it cannot invent one."""
        report = build_clusters([tx("0xa", (4, 5), (500, 300))])
        proposals, conflicts = propose_cluster_labels(report, self.SEED)
        assert proposals == [] and conflicts == []

    def test_a_fully_labelled_cluster_proposes_nothing_new(self) -> None:
        report = build_clusters([tx("0xa", (1, 2), (500, 300))])
        seeds = {**self.SEED, 2: ("Binance", "vasp", "etherscan-tags", 0.75)}
        proposals, _ = propose_cluster_labels(report, seeds)
        assert proposals == []

    def test_two_entities_in_one_cluster_is_refused_whole_not_broken(self) -> None:
        """Co-spending says ONE controller. Two exchanges inside one cluster
        means the clustering is wrong, so no name from it can be trusted —
        including the one that would 'win' a tie-break."""
        report = build_clusters([tx("0xa", (1, 2, 3), (500, 300))])
        seeds = {**self.SEED, 2: ("Kraken", "vasp", "etherscan-tags", 0.75)}
        proposals, conflicts = propose_cluster_labels(report, seeds)
        assert proposals == []
        assert conflicts == [("Binance", "Kraken")]

    def test_confidence_is_bounded_like_every_other_claim(self) -> None:
        with pytest.raises(ValueError, match="strictly inside"):
            ClusterProposal(
                entity="Binance",
                category="vasp",
                seed_address_ids=frozenset({1}),
                new_address_ids=frozenset({2}),
                source="etherscan-tags",
                confidence=1.0,
                cluster_size=2,
            )

    def test_a_proposal_cannot_exist_without_a_seed(self) -> None:
        with pytest.raises(ValueError, match="at least one seed"):
            ClusterProposal(
                entity="Binance",
                category="vasp",
                seed_address_ids=frozenset(),
                new_address_ids=frozenset({2}),
                source="etherscan-tags",
                confidence=0.7,
                cluster_size=2,
            )
