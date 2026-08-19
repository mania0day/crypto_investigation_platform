"""The asset-forgery attack, and the floor that closes it.

An ERC-20/TRC-20 contract may emit `Transfer` events between addresses that
never signed anything. So a third party can deploy a worthless token and, for
the price of gas on a dozen cheap transactions, manufacture a complete
receive-and-forward pattern for a victim of their choosing — pointed at
whatever destination they like.

Found by an adversarial review (docs/research/DEPOSIT_ADDRESS_DESIGN.md §2.1),
which ran these ledgers through the REAL `find_sweep_matches` and got 6/6
matches at ratio 1.000. These are permanent regression tests: this attack
surface must never silently reopen.

The defence is a provenance floor — a heuristic may only point at movements in
an asset CipherChain has verified (native, or a token contract confirmed on-chain
before shipping). Everything here asserts behaviour through the real engine.
"""

from cipherchain.analysis.assets import AssetPolicy, VerifiedAsset
from cipherchain.analysis.heuristics import ALL_DETECTORS, detect_service_endpoint
from cipherchain.core.models import FindingKind
from cipherchain.investigation import Budgets, InvestigationEngine, NullAttributor, Objective
from cipherchain.storage.repositories import InvestigationRepository
from tests.investigation.conftest import CHAIN, ROOT, FakeAdapter, Hop

# An attacker-deployed contract. Nothing verified it; nobody signed for it.
FORGED_TOKEN = "0xforged00000000000000000000000000000bad01"
# A contract whose provenance IS established (stands in for USDT/USDC).
VERIFIED_TOKEN = "0xverified000000000000000000000000000good1"

COLLECTOR = "collector"

VERIFIED_POLICY = AssetPolicy(
    [
        VerifiedAsset(
            chain=CHAIN,
            contract=VERIFIED_TOKEN,
            symbol="TKN",
            issuer="Test Issuer",
            source="test-pack@2026-08-09",
        )
    ]
)


def frame_ledger(token: str | None, cycles: int = 6) -> tuple[Hop, ...]:
    """The adversary's frame: alternating planted receipt and planted forward.

    `Transfer(random_i -> victim)` then `Transfer(victim -> collector)`, so the
    victim looks like a textbook pass-through into the collector.
    """
    hops: list[Hop] = []
    for i in range(cycles):
        hops.append(Hop(f"planted_{i}", ROOT, 1_000_000, 2 * i + 1, f"tx_in_{i}", token=token))
        hops.append(Hop(ROOT, COLLECTOR, 1_000_000, 2 * i + 2, f"tx_out_{i}", token=token))
    return tuple(hops)


async def run_trace(sessions, ledger, policy) -> list:
    adapter = FakeAdapter(ledger=ledger)
    from cipherchain.chains.base import ChainRegistry

    registry = ChainRegistry()
    registry.register(adapter)
    engine = InvestigationEngine(
        registry,
        sessions,
        NullAttributor(),
        detectors=ALL_DETECTORS,
        service_detector=detect_service_endpoint,
        evidence_assets=policy.accepts,
    )
    investigation_id = await engine.start(
        CHAIN, ROOT, (Objective.FIND_NEXT_VASP,), Budgets(api_calls=40, max_depth=2, max_nodes=200)
    )
    await engine.run(investigation_id)
    async with sessions() as session:
        return await InvestigationRepository(session).list_findings(investigation_id)


def behavioural(findings) -> list:
    """Findings that rest on a movement pattern — the forgeable kind."""
    return [
        f
        for f in findings
        if f.kind
        in (
            FindingKind.SWEEP_PATTERN,
            FindingKind.OBFUSCATION_PATTERN,
            FindingKind.VASP_ENDPOINT,
        )
    ]


async def test_the_attack_works_when_the_asset_is_trusted(sessions) -> None:
    """The premise. If this ever stops producing findings the other tests below
    are proving nothing, because the pattern itself stopped being detected."""
    findings = await run_trace(sessions, frame_ledger(token=None), VERIFIED_POLICY)
    assert behavioural(findings), (
        "the frame must produce behavioural findings when the asset is trusted — "
        "otherwise the negative tests are vacuous"
    )


async def test_forged_token_frame_produces_no_behavioural_finding(sessions) -> None:
    """Six planted cycles in an attacker-deployed token: the whole pattern is
    real events and entirely fabricated. It must found nothing."""
    findings = await run_trace(sessions, frame_ledger(FORGED_TOKEN, cycles=6), VERIFIED_POLICY)
    assert behavioural(findings) == [], (
        f"forged-token frame founded an inference: {[f.summary for f in behavioural(findings)]}"
    )


async def test_forged_token_frame_does_not_scale_its_way_in(sessions) -> None:
    """The adversary showed volume raises the score, because planted movements
    are near-free. Thirty cycles must be exactly as inadmissible as six."""
    findings = await run_trace(sessions, frame_ledger(FORGED_TOKEN, cycles=30), VERIFIED_POLICY)
    assert behavioural(findings) == []


async def test_verified_token_is_still_analysed(sessions) -> None:
    """The floor must not be a blanket ban on tokens — USDT sweeps are the most
    forensically important pattern on Tron."""
    findings = await run_trace(sessions, frame_ledger(VERIFIED_TOKEN, cycles=6), VERIFIED_POLICY)
    assert behavioural(findings), "a verified token must still support inference"


async def test_forged_movements_still_expand_the_graph(sessions) -> None:
    """The floor is a limit on EVIDENCE, not on traversal. The movements are
    real events; hiding them from the graph would be its own dishonesty."""
    adapter = FakeAdapter(ledger=frame_ledger(FORGED_TOKEN, cycles=6))
    from cipherchain.chains.base import ChainRegistry

    registry = ChainRegistry()
    registry.register(adapter)
    engine = InvestigationEngine(
        registry,
        sessions,
        NullAttributor(),
        detectors=ALL_DETECTORS,
        evidence_assets=VERIFIED_POLICY.accepts,
    )
    investigation_id = await engine.start(
        CHAIN, ROOT, (Objective.FIND_NEXT_VASP,), Budgets(api_calls=40, max_depth=2, max_nodes=200)
    )
    await engine.run(investigation_id)
    assert COLLECTOR in adapter.history_calls, (
        "the counterparty must still be reached — the floor governs evidence, not traversal"
    )


async def test_default_policy_is_fail_closed(sessions) -> None:
    """An engine wired without a policy must not be exploitable. Native only."""
    adapter = FakeAdapter(ledger=frame_ledger(FORGED_TOKEN, cycles=6))
    from cipherchain.chains.base import ChainRegistry

    registry = ChainRegistry()
    registry.register(adapter)
    engine = InvestigationEngine(
        registry, sessions, NullAttributor(), detectors=ALL_DETECTORS
    )  # no evidence_assets
    investigation_id = await engine.start(
        CHAIN, ROOT, (Objective.FIND_NEXT_VASP,), Budgets(api_calls=40, max_depth=2, max_nodes=200)
    )
    await engine.run(investigation_id)
    async with sessions() as session:
        findings = await InvestigationRepository(session).list_findings(investigation_id)
    assert behavioural(findings) == []


# ── traversal steering: the second surface of the same attack ────────────────

POISON_TOKENS = [
    # Six counterfeit contracts, each seen ONCE, each naming the identical
    # absurd amount — the exact address-poisoning shape measured on a live
    # Bybit-theft trace, where a fake "USDC" and an "mUSDT" were among them.
    "0x573d93903d4e955ae500000000000000000000f1",
    "0x89aaa9626a7cdad8f800000000000000000000f2",
    "0x008186658213ccd7c600000000000000000000f3",
    "0x3cd07baddc0d9a050600000000000000000000f4",
    "0xf00077fd8cb0647c7900000000000000000000f5",
    "0xea6da5092ff913e8c200000000000000000000f6",
]
ABSURD = 1_393_796_574_908_163_946_346_000_838_784_596_303_675_393
REAL_ETH = 322 * 10**18


async def test_spam_tokens_cannot_steer_the_traversal(sessions) -> None:
    """An attacker must not be able to choose what CipherChain explores first.

    Ranking on unverified assets handed the steering wheel to anyone willing to
    pay gas: a token contract can emit transfers naming ANY amount, so spraying
    a victim buries the real trail beneath the spam until the budget runs out.
    Measured live, the five highest-ranked unexplored branches were all
    unverified tokens at ~1e26 against a largest genuine movement of 3.22e20.
    """
    ledger = [Hop("origin", ROOT, REAL_ETH, 1, "tx_fund")]
    # The genuine onward movement — modest in nominal terms, real in substance.
    ledger.append(Hop(ROOT, "genuine_cashout", REAL_ETH, 2, "tx_real"))
    # The spray: six worthless tokens, each to its own address, each enormous.
    for index, token in enumerate(POISON_TOKENS):
        ledger.append(Hop(ROOT, f"spam_{index}", ABSURD, 3, f"tx_spam_{index}", token=token))

    adapter = FakeAdapter(ledger=tuple(ledger))
    from cipherchain.chains.base import ChainRegistry

    registry = ChainRegistry()
    registry.register(adapter)
    engine = InvestigationEngine(
        registry,
        sessions,
        NullAttributor(),
        detectors=ALL_DETECTORS,
        evidence_assets=VERIFIED_POLICY.accepts,
    )
    investigation_id = await engine.start(
        CHAIN, ROOT, (Objective.FIND_NEXT_VASP,), Budgets(api_calls=40, max_depth=2, max_nodes=200)
    )
    await engine.run(investigation_id)

    from sqlalchemy import text

    async with sessions() as session:
        rows = (
            await session.execute(
                text(
                    "select a.address, n.value_share from nodes n "
                    "join addresses a on a.id = n.address_id "
                    "where n.hop_distance = 1 order by n.value_share desc nulls last"
                )
            )
        ).all()

    ranked = [(r[0], int(r[1] or 0)) for r in rows]
    assert ranked, "hop-1 counterparties should exist"
    assert ranked[0][0] == "genuine_cashout", f"the real branch must rank first; got {ranked[:3]}"
    spam_values = [value for address, value in ranked if address.startswith("spam_")]
    assert spam_values and all(v == 0 for v in spam_values), (
        f"unverified-token branches must not carry ranking value; got {spam_values}"
    )


async def test_spam_movements_are_still_recorded_and_still_explored(sessions) -> None:
    """The fix governs ORDER only.

    Forged transfers are real on-chain events. They must stay in the graph and
    remain reachable — suppressing them would be the dishonesty the provenance
    floor exists to avoid, and would hide an attacker's own footprints.
    """
    ledger = [Hop("origin", ROOT, REAL_ETH, 1, "tx_fund")]
    for index, token in enumerate(POISON_TOKENS[:3]):
        ledger.append(Hop(ROOT, f"spam_{index}", ABSURD, 3, f"tx_spam_{index}", token=token))

    adapter = FakeAdapter(ledger=tuple(ledger))
    from cipherchain.chains.base import ChainRegistry

    registry = ChainRegistry()
    registry.register(adapter)
    engine = InvestigationEngine(
        registry, sessions, NullAttributor(), evidence_assets=VERIFIED_POLICY.accepts
    )
    investigation_id = await engine.start(
        CHAIN, ROOT, (Objective.FIND_NEXT_VASP,), Budgets(api_calls=40, max_depth=2, max_nodes=200)
    )
    await engine.run(investigation_id)

    from sqlalchemy import text

    async with sessions() as session:
        addresses = {
            r[0]
            for r in (
                await session.execute(
                    text("select a.address from nodes n join addresses a on a.id = n.address_id")
                )
            ).all()
        }
        movements = (await session.execute(text("select count(*) from movements"))).scalar()

    for index in range(3):
        assert f"spam_{index}" in addresses, (
            "a forged-asset counterparty must still enter the graph — it is a real event"
        )
    assert movements >= 4, "forged movements must still be stored"
    assert "spam_0" in adapter.history_calls, "and must still be reachable for expansion"
