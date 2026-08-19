"""Five defects the deposit-address review surfaced in SHIPPED code.

Three were live at HEAD and reachable in ordinary runs, with no new feature and
no cache involved. Each test states the wrong behaviour it exists to prevent.
"""

import pytest

from cipherchain.analysis.attribution.labels import LabelPack, LabelRecord
from cipherchain.analysis.attribution.store import LabelStoreAttributor
from cipherchain.analysis.heuristics import detect_service_endpoint
from cipherchain.chains.base import ChainRegistry
from cipherchain.core.models import Direction, EvidenceKind, FindingKind
from cipherchain.investigation import Budgets, InvestigationEngine, NullAttributor, Objective
from cipherchain.investigation.attribution import AddressRole
from cipherchain.storage.repositories import InvestigationRepository
from tests.investigation.conftest import CHAIN, ROOT, FakeAdapter, Hop


def pack(*records: LabelRecord) -> LabelPack:
    return LabelPack(name="test-pack", labels=records)


def vasp_label(address: str, entity: str, role: AddressRole) -> LabelRecord:
    return LabelRecord(
        chain=CHAIN,
        address=address,
        entity=entity,
        category="vasp",
        source="test-pack@2026-08-10",
        confidence=0.75,
        role=role,
    )


# ── defect 3: confidence == 1.0 ──────────────────────────────────────────────


def test_a_label_may_not_claim_certainty() -> None:
    """Evidence already rejects a 1.0 third-party claim, so admitting it here
    only deferred the failure to mid-run, on the attribution path."""
    with pytest.raises(ValueError, match="not truth"):
        LabelRecord(
            chain=CHAIN,
            address="0xabc",
            entity="Acme",
            category="vasp",
            source="s",
            confidence=1.0,
        )


# ── defect 4: role is a declared field, not a string convention ──────────────


def test_role_defaults_to_unknown_not_to_a_guess() -> None:
    record = LabelRecord(
        chain=CHAIN, address="0xabc", entity="Acme", category="vasp", source="s", confidence=0.8
    )
    assert record.role is AddressRole.UNKNOWN, (
        "a pack that does not declare a role has not said 'operational'"
    )


async def test_role_reaches_the_finding_text(sessions) -> None:
    """A deposit address names an ACCOUNT; a collector names only the operator.
    A report must not have to parse an entity string to tell them apart."""
    ledger = (Hop(ROOT, "intake", 900, 2, "tx_out"),)
    registry = ChainRegistry()
    registry.register(FakeAdapter(ledger=ledger))
    attributor = LabelStoreAttributor([pack(vasp_label("intake", "Bitget", AddressRole.DEPOSIT))])
    engine = InvestigationEngine(registry, sessions, attributor)
    investigation_id = await engine.start(CHAIN, ROOT, (Objective.FIND_NEXT_VASP,))
    await engine.run(investigation_id)

    async with sessions() as session:
        findings = await InvestigationRepository(session).list_findings(investigation_id)
    vasp = next(f for f in findings if f.kind is FindingKind.VASP_ENDPOINT)
    assert "customer deposit address" in vasp.summary
    assert "identify the account" in vasp.summary


# ── defect 2: an unnamed inference must not close an objective ───────────────


async def test_an_unnamed_service_inference_does_not_answer_the_objective(sessions) -> None:
    """`service-endpoint@1` says "operator unnamed" in its own summary. Letting
    it mark the objective answered suppressed the honest terminal, so a report
    showed a confident endpoint where the tool had named nobody.
    """
    ledger = tuple(
        [Hop(f"payer_{i}", "hub", 100 + i, 1, f"tx_in_{i}") for i in range(30)]
        + [Hop("hub", f"payee_{i}", 90 + i, 2, f"tx_out_{i}") for i in range(30)]
        + [Hop(ROOT, "hub", 500, 1, "tx_root")]
    )
    registry = ChainRegistry()
    registry.register(FakeAdapter(ledger=ledger))
    engine = InvestigationEngine(
        registry,
        sessions,
        NullAttributor(),
        service_detector=detect_service_endpoint,
    )
    investigation_id = await engine.start(
        CHAIN, ROOT, (Objective.FIND_NEXT_VASP,), Budgets(api_calls=30, max_depth=3)
    )
    await engine.run(investigation_id)

    async with sessions() as session:
        findings = await InvestigationRepository(session).list_findings(investigation_id)

    inferred = [
        f
        for f in findings
        if f.kind is FindingKind.VASP_ENDPOINT
        and not any(e.kind is EvidenceKind.THIRD_PARTY_CLAIM for e in f.evidence)
    ]
    assert inferred, "the service detector should have fired on this shape"
    terminals = {f.direction for f in findings if f.kind is FindingKind.TERMINAL}
    assert Direction.FORWARD in terminals, (
        "an unnamed operator must not suppress the honest terminal"
    )
    # Pick the terminal for the DIRECTION under test: a run may also file an
    # undirected one naming the budget that stopped it, and that is a different
    # statement. The directed one is what says the operator was never named.
    terminal = next(
        f for f in findings if f.kind is FindingKind.TERMINAL and f.direction is Direction.FORWARD
    )
    assert "unnamed" in terminal.summary


async def test_a_sourced_label_does_answer_the_objective(sessions) -> None:
    """The other half: a real claim still closes the objective, so the fix above
    did not simply stop objectives ever being answered."""
    ledger = (Hop(ROOT, "exchange", 900, 2, "tx_out"),)
    registry = ChainRegistry()
    registry.register(FakeAdapter(ledger=ledger))
    attributor = LabelStoreAttributor(
        [pack(vasp_label("exchange", "Acme", AddressRole.OPERATIONAL))]
    )
    engine = InvestigationEngine(registry, sessions, attributor)
    investigation_id = await engine.start(CHAIN, ROOT, (Objective.FIND_NEXT_VASP,))
    await engine.run(investigation_id)

    async with sessions() as session:
        findings = await InvestigationRepository(session).list_findings(investigation_id)
    assert any(f.kind is FindingKind.VASP_ENDPOINT for f in findings)
    assert not [
        f for f in findings if f.kind is FindingKind.TERMINAL and f.direction is Direction.FORWARD
    ], "a sourced endpoint answers the objective, so no terminal for that direction"


# ── defect 5: a labelled ROOT is recorded, then still expanded ───────────────


async def test_a_labelled_root_is_recorded_but_still_traced(sessions) -> None:
    """Terminating at hop 0 answered "where did these funds go?" with "this
    address is Acme" and stopped — true, and useless, since the investigator
    supplied the address precisely to see its flows."""
    ledger = (
        Hop(ROOT, "cashout", 900, 2, "tx_out"),
        Hop("cashout", "exchange_out", 800, 3, "tx_ex"),
    )
    adapter = FakeAdapter(ledger=ledger)
    registry = ChainRegistry()
    registry.register(adapter)
    attributor = LabelStoreAttributor([pack(vasp_label(ROOT, "Acme", AddressRole.OPERATIONAL))])
    engine = InvestigationEngine(registry, sessions, attributor)
    investigation_id = await engine.start(CHAIN, ROOT, (Objective.FIND_NEXT_VASP,))
    await engine.run(investigation_id)

    async with sessions() as session:
        findings = await InvestigationRepository(session).list_findings(investigation_id)
    assert any(
        f.kind is FindingKind.VASP_ENDPOINT and "root address" in f.summary for f in findings
    ), "the root's own attribution must still be recorded"
    assert "cashout" in adapter.history_calls, (
        "a labelled root must still be expanded — otherwise the trace never happens"
    )


# ── a sourced label beats a behavioural guess ────────────────────────────────


async def test_a_labelled_dex_is_not_inferred_to_be_custodial(sessions) -> None:
    """Found on a real Bybit-theft trace: CoW Protocol's GPv2Settlement contract
    was reported as "custodial infrastructure such as an exchange".

    A busy settlement contract has an exchange's counterparty degree and none of
    the custody, so degree alone cannot tell them apart. A sourced label saying
    "this is a DEX router" must beat the behavioural guess.
    """
    from cipherchain.analysis.heuristics import detect_service_endpoint
    from cipherchain.investigation.attribution import CATEGORY_INFRASTRUCTURE

    hub = "settlement"
    ledger = tuple(
        [Hop(f"payer_{i}", hub, 100 + i, 1, f"tx_in_{i}") for i in range(30)]
        + [Hop(hub, f"payee_{i}", 90 + i, 2, f"tx_out_{i}") for i in range(30)]
        + [Hop(ROOT, hub, 500, 1, "tx_root")]
    )
    registry = ChainRegistry()
    registry.register(FakeAdapter(ledger=ledger))

    def engine_with(attributor):
        return InvestigationEngine(
            registry, sessions, attributor, service_detector=detect_service_endpoint
        )

    # Without the label the detector fires — the premise of the test.
    unlabelled = engine_with(NullAttributor())
    first = await unlabelled.start(
        CHAIN, ROOT, (Objective.FIND_NEXT_VASP,), Budgets(api_calls=30, max_depth=3)
    )
    await unlabelled.run(first)
    async with sessions() as session:
        findings = await InvestigationRepository(session).list_findings(first)
    assert [f for f in findings if f.kind is FindingKind.VASP_ENDPOINT], (
        "the service detector must fire here, or the negative case proves nothing"
    )

    # With it, the inference is suppressed.
    labelled = engine_with(
        LabelStoreAttributor(
            [
                pack(
                    LabelRecord(
                        chain=CHAIN,
                        address=hub,
                        entity="CoW Protocol: GPv2Settlement",
                        category=CATEGORY_INFRASTRUCTURE,
                        source="etherscan-tags@2026-07-10",
                        confidence=0.75,
                    )
                )
            ]
        )
    )
    second = await labelled.start(
        CHAIN, ROOT, (Objective.FIND_NEXT_VASP,), Budgets(api_calls=30, max_depth=3)
    )
    await labelled.run(second)
    async with sessions() as session:
        findings = await InvestigationRepository(session).list_findings(second)
    assert not [f for f in findings if f.kind is FindingKind.VASP_ENDPOINT], (
        "a labelled DEX must never be inferred as a custodial endpoint"
    )
