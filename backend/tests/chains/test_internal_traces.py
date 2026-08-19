"""Contract-delivered native value must be visible.

Fixtures recorded live 2026-08-09 from a real wallet whose entire native
funding arrives as internal traces from the sanctioned Tornado Cash 0.1 ETH
pool. Its ``txlist`` contains no incoming native value at all — which is
exactly why the trail used to appear to end there.
"""

from cipherchain.chains.evm import ETHEREUM_CONFIG, EvmAdapter
from cipherchain.core.models import Address, MovementKind
from tests.chains.conftest import MIXER_FUNDED_ADDRESS, TORNADO_01_ETH_POOL, fixture_json


def test_the_funding_really_is_invisible_in_txlist() -> None:
    """The premise of this whole test module, asserted against the fixture."""
    rows = fixture_json("eth_internal_mixer_txlist.json")["result"]
    incoming_native = [
        r for r in rows if str(r["to"]).lower() == MIXER_FUNDED_ADDRESS and int(r["value"]) > 0
    ]
    assert rows, "fixture should not be empty"
    assert incoming_native == [], "txlist must show no incoming native value for this wallet"


async def test_internal_traces_reveal_the_mixer_counterparty(fixture_pool) -> None:
    """The regression: before internal traces were requested, this wallet had
    no visible funder and the engine reported the trail as ending here."""
    adapter = EvmAdapter(ETHEREUM_CONFIG, fixture_pool)
    page = await adapter.address_history(Address("ethereum", MIXER_FUNDED_ADDRESS), limit=20)

    senders: set[str] = set()
    internal_movements = 0
    for item in page.items:
        normalized = await adapter.normalize(item)
        for movement in normalized.movements:
            if movement.kind is MovementKind.INTERNAL:
                internal_movements += 1
                assert movement.from_address is not None
                assert movement.to_address is not None
                senders.add(movement.from_address.value)

    assert internal_movements > 0, "internal traces produced no movements"
    assert TORNADO_01_ETH_POOL in senders, (
        f"the sanctioned mixer pool must appear as a counterparty; got senders={sorted(senders)}"
    )


async def test_internal_movements_carry_stable_dedup_keys(fixture_pool) -> None:
    """Re-normalizing the same transaction must key identically, or the same
    transfer would be stored twice (REVIEW_FINDINGS.md #1)."""
    adapter = EvmAdapter(ETHEREUM_CONFIG, fixture_pool)
    page = await adapter.address_history(Address("ethereum", MIXER_FUNDED_ADDRESS), limit=20)

    for item in page.items:
        first = await adapter.normalize(item)
        second = await adapter.normalize(item)
        keys = [(m.kind, m.dedup_key) for m in first.movements]
        assert keys == [(m.kind, m.dedup_key) for m in second.movements]
        # Within one transaction, identity must be unique or storage dedups
        # two distinct transfers into one.
        assert len(set(keys)) == len(keys), f"duplicate dedup_key in {item.tx_hash}"


async def test_failed_and_zero_value_internal_calls_move_nothing(fixture_pool) -> None:
    """A reverted internal call, and a zero-value one, transfer no value and
    must not create a counterparty edge."""
    adapter = EvmAdapter(ETHEREUM_CONFIG, fixture_pool)
    rows = fixture_json("eth_internal_mixer.json")["result"]
    tx_hash = str(rows[0]["hash"]).lower()

    from cipherchain.chains.base import ChainTransaction

    # Borrow a real provenance rather than fabricate one, so this test breaks if
    # the acquisition path stops carrying provenance at all.
    live = await adapter.address_history(Address("ethereum", MIXER_FUNDED_ADDRESS), limit=20)
    provenance = live.items[0].provenance

    poisoned = [
        {**rows[0], "isError": "1", "traceId": "9_9"},  # reverted call
        {**rows[0], "value": "0", "traceId": "9_8"},  # moved nothing
        {**rows[0], "from": "", "traceId": "9_7"},  # no origin
        {**rows[0], "to": "", "contractAddress": "", "traceId": "9_6"},  # no target
    ]
    tx = ChainTransaction(
        chain="ethereum",
        tx_hash=tx_hash,
        raw={
            "source": "etherscan",
            "txlist_row": None,
            "token_rows": [],
            "internal_rows": poisoned,
            "prov_txlist": None,
            "prov_token": None,
            "prov_internal": provenance,
        },
        provenance=provenance,
    )
    normalized = await adapter.normalize(tx)
    assert normalized.movements == (), "no poisoned internal row may produce a movement"
