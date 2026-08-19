"""A guessed branch must never be storable as a traced one.

Following a mixer produces nodes that are inferences about which withdrawal
belongs to which deposit (REACHING_THE_VASP.md §3). These tests lock down the
three properties that make that safe to do at all: the flag and the heuristic
that set it cannot come apart, clean branches are always claimed first, and the
read model a picture is drawn from carries the speculation through.
"""

import uuid

import pytest
from sqlalchemy import text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from cipherchain.core.models import (
    Address,
    Direction,
    Evidence,
    EvidenceKind,
    Finding,
    FindingKind,
)
from cipherchain.investigation.answers import RankedFinding, select_answers
from cipherchain.storage.repositories import FactRepository, InvestigationRepository

MIXER_EXIT = "mixer-exit-address-match@1"


async def new_investigation(
    session: AsyncSession,
) -> tuple[InvestigationRepository, uuid.UUID]:
    facts = FactRepository(session)
    root_id = await facts.get_or_create_address(Address("ethereum", "0xroot"))
    repo = InvestigationRepository(session)
    row = await repo.create(
        root_address_id=root_id,
        objectives=["find_prev_vasp"],
        budgets={"api_calls": 100, "seconds": 300, "max_depth": 4, "max_nodes": 500},
        engine_version="0.1.0",
        ruleset_version="2026-08-16",
    )
    return repo, row.id


async def add_node(
    session: AsyncSession,
    repo: InvestigationRepository,
    investigation_id: uuid.UUID,
    address: str,
    *,
    hop_distance: int = 1,
    value_share: int | None = 10,
    speculative_basis: str | None = None,
) -> int:
    address_id = await FactRepository(session).get_or_create_address(Address("ethereum", address))
    node_id = await repo.add_address_node(
        investigation_id,
        address_id,
        direction=Direction.BACKWARD,
        hop_distance=hop_distance,
        value_share=value_share,
        discovered_reason="find_prev_vasp",
        speculative_basis=speculative_basis,
    )
    assert node_id is not None
    return node_id


async def speculation_of(session: AsyncSession, node_id: int) -> tuple[bool, str | None]:
    row = (
        await session.execute(
            text("SELECT speculative, speculative_basis FROM nodes WHERE id = :id"),
            {"id": node_id},
        )
    ).one()
    return bool(row[0]), row[1]


class TestSpeculationIsRecordedWithItsBasis:
    async def test_a_node_created_from_a_heuristic_names_that_heuristic(
        self, session: AsyncSession
    ) -> None:
        """Speculation is set at INSERT, not by a follow-up update: a crash
        between the two would checkpoint a mixer-derived branch as a clean one,
        and a resumed run would then trust it."""
        repo, investigation_id = await new_investigation(session)
        node_id = await add_node(
            session, repo, investigation_id, "0xcandidate", speculative_basis=MIXER_EXIT
        )
        assert await speculation_of(session, node_id) == (True, MIXER_EXIT)

    async def test_an_ordinary_traced_node_is_not_speculative(self, session: AsyncSession) -> None:
        repo, investigation_id = await new_investigation(session)
        node_id = await add_node(session, repo, investigation_id, "0xtraced")
        assert await speculation_of(session, node_id) == (False, None)

    async def test_marking_a_node_records_which_heuristic_proposed_it(
        self, session: AsyncSession
    ) -> None:
        """Descendants of a mixer crossing are marked after the fact, inheriting
        the ancestor's basis — the report cites the guess the whole branch rests
        on, not merely that one was made."""
        repo, investigation_id = await new_investigation(session)
        node_id = await add_node(session, repo, investigation_id, "0xdescendant")
        await repo.mark_node_speculative(node_id, basis=MIXER_EXIT)
        assert await speculation_of(session, node_id) == (True, MIXER_EXIT)

    async def test_marking_a_node_with_an_empty_basis_is_refused(
        self, session: AsyncSession
    ) -> None:
        """Rejected in Python rather than left to the CHECK, which an empty
        string satisfies: '' is not null, so the DB would accept a flag that
        explains nothing."""
        repo, investigation_id = await new_investigation(session)
        node_id = await add_node(session, repo, investigation_id, "0xdescendant")
        with pytest.raises(ValueError):
            await repo.mark_node_speculative(node_id, basis="")

    async def test_a_node_cannot_be_CREATED_speculative_without_naming_the_heuristic(
        self, session: AsyncSession
    ) -> None:
        """The insert path has to hold the same line the mark path does. It did
        not: `speculative_basis=''` derived the flag from "is not None" and
        wrote (speculative=true, basis=''), satisfying the NULL equivalence and
        producing the one node this pair exists to make unrepresentable — and
        by the path the design calls the safe one, since it is the path that
        survives a crash."""
        repo, investigation_id = await new_investigation(session)
        with pytest.raises(ValueError):
            await add_node(session, repo, investigation_id, "0xblank", speculative_basis="")

    async def test_a_basis_of_only_whitespace_is_not_an_explanation(
        self, session: AsyncSession
    ) -> None:
        """`if not basis` is true for '' and false for '   '. A report citing a
        heuristic id of three spaces states a guess it cannot attribute."""
        repo, investigation_id = await new_investigation(session)
        node_id = await add_node(session, repo, investigation_id, "0xdescendant")
        with pytest.raises(ValueError):
            await repo.mark_node_speculative(node_id, basis="   ")
        with pytest.raises(ValueError):
            await add_node(session, repo, investigation_id, "0xspaces", speculative_basis="  ")

    async def test_marking_a_node_that_does_not_exist_is_an_error_not_a_no_op(
        self, session: AsyncSession
    ) -> None:
        """This is the PROPAGATION path — it walks a subtree marking the
        descendants of a mixer crossing. A silent no-op on a stale id leaves
        that descendant flagged as traced while the caller believes it was
        marked, which is the failure the whole column exists to prevent, so it
        raises rather than returning a boolean nobody must read."""
        repo, _ = await new_investigation(session)
        with pytest.raises(LookupError):
            await repo.mark_node_speculative(999_999, basis=MIXER_EXIT)


class TestTheFlagAndItsBasisCannotComeApart:
    async def test_a_speculative_node_cannot_hide_which_heuristic_guessed_it(
        self, session: AsyncSession
    ) -> None:
        """Raw SQL on purpose: the constraint has to hold against the
        propagation path too, which is the easy place to carry one field and
        forget the other. A guess nobody can attribute cannot go into a
        document that reaches a regulator."""
        repo, investigation_id = await new_investigation(session)
        node_id = await add_node(session, repo, investigation_id, "0xguess")
        with pytest.raises(IntegrityError):
            await session.execute(
                text("UPDATE nodes SET speculative = true WHERE id = :id"), {"id": node_id}
            )

    async def test_a_basis_cannot_sit_on_a_node_drawn_as_traced(
        self, session: AsyncSession
    ) -> None:
        """The worse direction of the same equivalence: a heuristic proposed
        this node, but the flag says traced, so every renderer and every ranking
        treats a guess as an observed movement."""
        repo, investigation_id = await new_investigation(session)
        node_id = await add_node(session, repo, investigation_id, "0xguess")
        with pytest.raises(IntegrityError):
            await session.execute(
                text("UPDATE nodes SET speculative_basis = :basis WHERE id = :id"),
                {"basis": MIXER_EXIT, "id": node_id},
            )

    async def test_the_db_refuses_a_flag_whose_basis_explains_nothing(
        self, session: AsyncSession
    ) -> None:
        """The NULL equivalence alone cannot catch this: '' and '   ' are both
        non-null, so both satisfy it. Held in the DB and not only in Python
        because the propagation path is the easy place to carry a field and
        forget what is in it."""
        repo, investigation_id = await new_investigation(session)
        node_id = await add_node(session, repo, investigation_id, "0xguess")
        # A savepoint per attempt, not session.rollback(): a full rollback would
        # also discard the node this test just created, and the second UPDATE
        # would then match zero rows and "pass" by touching nothing.
        for blank in ("", "   "):
            with pytest.raises(IntegrityError):
                async with session.begin_nested():
                    await session.execute(
                        text(
                            "UPDATE nodes SET speculative = true, speculative_basis = :b"
                            " WHERE id = :id"
                        ),
                        {"b": blank, "id": node_id},
                    )


class TestFrontierOrder:
    async def test_clean_branches_are_exhausted_before_any_mixer_candidate(
        self, session: AsyncSession
    ) -> None:
        """`speculative` sorts ahead of hop distance, so a guess one hop out
        still waits behind a traced node five hops out — and value share, which
        outranks nothing here, cannot buy it a place either."""
        repo, investigation_id = await new_investigation(session)
        near_guess = await add_node(
            session,
            repo,
            investigation_id,
            "0xcandidate",
            hop_distance=1,
            value_share=10**9,
            speculative_basis=MIXER_EXIT,
        )
        far_clean = await add_node(
            session, repo, investigation_id, "0xfar", hop_distance=5, value_share=1
        )
        claimed = await repo.claim_frontier(investigation_id, limit=10)
        assert [node.id for node in claimed] == [far_clean, near_guess]


class TestGraphReadModel:
    async def test_the_picture_is_told_which_branches_are_guesses(
        self, session: AsyncSession
    ) -> None:
        """A renderer that cannot see `speculative` draws a guess as a traced
        path — the single outcome that makes following a mixer unsafe."""
        repo, investigation_id = await new_investigation(session)
        await add_node(session, repo, investigation_id, "0xtraced", hop_distance=1)
        await add_node(
            session,
            repo,
            investigation_id,
            "0xcandidate",
            hop_distance=2,
            speculative_basis=MIXER_EXIT,
        )
        nodes = {node.address: node for node in await repo.graph_nodes(investigation_id)}
        assert (nodes["0xtraced"].speculative, nodes["0xtraced"].speculative_basis) == (False, None)
        assert (nodes["0xcandidate"].speculative, nodes["0xcandidate"].speculative_basis) == (
            True,
            MIXER_EXIT,
        )


class TestTheAnswerLayerIsToldWhichEndpointsAreGuesses:
    """The seam between the graph and the answer.

    ``speculative`` is a property of the PATH, and a ``Finding`` records none of
    it: a mixer-exit branch can reach an address a sourced label names perfectly
    well, and the label stays true while the path to it remains a guess. If this
    join drops the flag, ``RankedFinding.speculative`` defaults to ``False`` and
    ``select_answers`` — which is otherwise correct — files the endpoint under a
    heading that asserts the trail was followed. Nothing raises, no test of the
    answer layer fails, and the report is wrong in the one way
    REACHING_THE_VASP.md §3 exists to prevent.
    """

    @staticmethod
    async def vasp_finding_on(
        session: AsyncSession,
        repo: InvestigationRepository,
        investigation_id: uuid.UUID,
        address: str,
        *,
        speculative_basis: str | None = None,
    ) -> None:
        await add_node(
            session, repo, investigation_id, address, speculative_basis=speculative_basis
        )
        address_id = await FactRepository(session).get_or_create_address(
            Address("ethereum", address)
        )
        await repo.add_finding(
            investigation_id,
            Finding(
                kind=FindingKind.VASP_ENDPOINT,
                subject=Address(chain="ethereum", value=address),
                summary=f"possible previous VASP: {address}",
                confidence=0.6,
                evidence=(
                    Evidence(
                        kind=EvidenceKind.THIRD_PARTY_CLAIM,
                        summary="Kraken (deposit address)",
                        source="etherscan-tags@2026-08-10",
                        confidence=0.9,
                    ),
                ),
                direction=Direction.BACKWARD,
            ),
            subject_address_id=address_id,
        )

    async def test_a_finding_on_a_guessed_branch_carries_the_guess_across_the_boundary(
        self, session: AsyncSession
    ) -> None:
        repo, investigation_id = await new_investigation(session)
        await self.vasp_finding_on(
            session, repo, investigation_id, "0xpastmixer", speculative_basis=MIXER_EXIT
        )

        [ranked] = await repo.vasp_findings_with_hops(investigation_id)

        assert ranked.speculative is True
        assert ranked.speculative_basis == MIXER_EXIT

    async def test_a_finding_on_a_traced_branch_is_not_marked_as_a_guess(
        self, session: AsyncSession
    ) -> None:
        """The other direction of the same seam: hard-coding speculative=True
        would pass the test above and bar every real answer from `nearest`."""
        repo, investigation_id = await new_investigation(session)
        await self.vasp_finding_on(session, repo, investigation_id, "0xtracedvasp")

        [ranked] = await repo.vasp_findings_with_hops(investigation_id)

        assert ranked.speculative is False
        assert ranked.speculative_basis is None

    async def test_the_selected_answer_keeps_a_mixer_exit_out_of_the_nearest_slot(
        self, session: AsyncSession
    ) -> None:
        """End to end through the real join, because this is where it broke.

        Both endpoints carry the same sourced label; only the path differs. The
        traced one is FOUR hops out and the guess is one — so a selection that
        cannot see speculation picks the guess, which is exactly what "nearest"
        would have meant before mixer crossing existed.
        """
        repo, investigation_id = await new_investigation(session)
        await self.vasp_finding_on(
            session, repo, investigation_id, "0xpastmixer", speculative_basis=MIXER_EXIT
        )
        await add_node(session, repo, investigation_id, "0xfartraced", hop_distance=4)
        far_id = await FactRepository(session).get_or_create_address(
            Address("ethereum", "0xfartraced")
        )
        await repo.add_finding(
            investigation_id,
            Finding(
                kind=FindingKind.VASP_ENDPOINT,
                subject=Address(chain="ethereum", value="0xfartraced"),
                summary="nearest previous VASP: 0xfartraced",
                confidence=0.9,
                evidence=(
                    Evidence(
                        kind=EvidenceKind.THIRD_PARTY_CLAIM,
                        summary="Binance (hot wallet)",
                        source="etherscan-tags@2026-08-10",
                        confidence=0.9,
                    ),
                ),
                direction=Direction.BACKWARD,
            ),
            subject_address_id=far_id,
        )

        ranked = await repo.vasp_findings_with_hops(investigation_id)
        [answer] = select_answers(
            [
                RankedFinding(
                    finding=r.finding,
                    hop=r.hop,
                    speculative=r.speculative,
                    speculative_basis=r.speculative_basis,
                )
                for r in ranked
            ],
            [Direction.BACKWARD],
        )

        assert answer.nearest is not None
        assert answer.nearest.finding.subject.value == "0xfartraced", (
            "a one-hop guess outranked a four-hop traced endpoint — "
            "the speculative flag did not survive the storage boundary"
        )
        # And no lead beside it: `DirectionAnswer` refuses the pair outright, so
        # the guess is dropped rather than offered next to a traced answer.
        assert answer.best_effort is None

    async def test_a_lone_mixer_exit_is_offered_as_a_lead_with_the_engines_own_basis(
        self, session: AsyncSession
    ) -> None:
        """The reason the flag must not simply suppress the endpoint.

        With the bar in place and nothing else found, a run that crossed a mixer
        would report nothing at all — the reader is handed an empty box while
        the run holds a name. The lead slot is what keeps it, and the weakness
        has to quote the run's actual reasoning rather than a sentence written
        at the edge, so the basis must survive the same join.
        """
        repo, investigation_id = await new_investigation(session)
        await self.vasp_finding_on(
            session, repo, investigation_id, "0xpastmixer", speculative_basis=MIXER_EXIT
        )

        ranked = await repo.vasp_findings_with_hops(investigation_id)
        [answer] = select_answers(
            [
                RankedFinding(
                    finding=r.finding,
                    hop=r.hop,
                    speculative=r.speculative,
                    speculative_basis=r.speculative_basis,
                )
                for r in ranked
            ],
            [Direction.BACKWARD],
        )

        assert answer.nearest is None
        assert answer.nearest_named is None
        assert answer.best_effort is not None
        assert answer.best_effort.finding.subject.value == "0xpastmixer"
        assert MIXER_EXIT in answer.best_effort.weakness
