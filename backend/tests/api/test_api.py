"""API smoke tests: the endpoints drive a real investigation end-to-end
through the ASGI app, against real Postgres, with a synthetic chain.

The client carries a real minted key (``conftest.client``), so every request
here also passes through the auth dependency the routes are mounted behind.
"""

import uuid

import httpx
import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from cipherchain.chains.base import FeedGap
from cipherchain.core.models import Capability
from cipherchain.storage.repositories import InvestigationRepository
from tests.investigation.conftest import CHAIN, EXCHANGE_IN, EXCHANGE_OUT, ROOT


async def test_healthz(client: httpx.AsyncClient) -> None:
    response = await client.get("/healthz")
    assert response.status_code == 200
    assert response.json() == {"status": "ok"}


async def test_full_investigation_lifecycle(client: httpx.AsyncClient) -> None:
    start = await client.post(
        "/investigations",
        json={
            "chain": CHAIN,
            "address": ROOT,
            "objectives": ["find_prev_vasp", "find_next_vasp"],
            "budgets": {"api_calls": 100, "seconds": 300, "max_depth": 6, "max_nodes": 500},
        },
    )
    assert start.status_code == 201
    body = start.json()
    investigation_id = body["investigation_id"]
    assert body["status"] == "completed"  # ran synchronously

    status = await client.get(f"/investigations/{investigation_id}")
    assert status.status_code == 200
    status_body = status.json()
    assert status_body["chain"] == CHAIN
    assert status_body["root_address"] == ROOT
    assert set(status_body["objectives"]) == {"find_prev_vasp", "find_next_vasp"}
    assert status_body["spent"]["api_calls"] >= 1

    findings = await client.get(f"/investigations/{investigation_id}/findings")
    assert findings.status_code == 200
    findings_body = findings.json()
    vasp = {f["direction"]: f for f in findings_body["findings"] if f["kind"] == "vasp_endpoint"}
    assert set(vasp) == {"backward", "forward"}
    assert vasp["backward"]["subject_address"] == EXCHANGE_IN
    assert vasp["forward"]["subject_address"] == EXCHANGE_OUT
    # evidence rides through the API in its taxonomy shape
    kinds = {e["kind"] for e in vasp["backward"]["evidence"]}
    assert "third_party_claim" in kinds and "onchain_fact" in kinds


async def test_answers_report_both_the_nearest_and_the_nearest_named(
    client: httpx.AsyncClient,
) -> None:
    """The API — not the frontend — decides what answers an objective.

    Every consumer that states "nearest previous/next VASP" must state the same
    thing, so the selection lives server-side. Here the exchanges ARE the
    nearest endpoints, so each direction collapses to a single named answer.
    """
    start = await client.post(
        "/investigations",
        json={
            "chain": CHAIN,
            "address": ROOT,
            "objectives": ["find_prev_vasp", "find_next_vasp"],
        },
    )
    investigation_id = start.json()["investigation_id"]

    body = (await client.get(f"/investigations/{investigation_id}/findings")).json()
    answers = {a["direction"]: a for a in body["answers"]}
    assert set(answers) == {"backward", "forward"}

    backward = answers["backward"]
    assert backward["nearest"]["address"] == EXCHANGE_IN
    assert backward["nearest"]["named"] is True
    assert backward["nearest"]["claim"], "a named endpoint carries the claim's own words"
    assert backward["nearest"]["hop"] > 0
    # Nearest IS the named one here — a consumer must print it once, not twice.
    assert backward["same"] is True
    assert backward["nearest_named"]["address"] == EXCHANGE_IN

    assert answers["forward"]["nearest"]["address"] == EXCHANGE_OUT
    assert answers["forward"]["same"] is True


async def test_answers_keep_a_named_endpoint_that_is_not_the_nearest(
    client: httpx.AsyncClient,
) -> None:
    """The failure shape this rule exists for, end to end.

    A run that infers custodial infrastructure nearby AND names an exchange
    further out must report both. Reporting only the nearest would hide the
    exchange behind a guess; reporting only the named one would drop the
    nearest. Which of those happened used to depend on traversal order.
    """
    start = await client.post(
        "/investigations",
        json={"chain": CHAIN, "address": ROOT, "objectives": ["find_prev_vasp"]},
    )
    investigation_id = start.json()["investigation_id"]
    body = (await client.get(f"/investigations/{investigation_id}/findings")).json()
    answer = next(a for a in body["answers"] if a["direction"] == "backward")

    # Whatever the fixture produced, the invariant holds: a named answer is
    # never silently replaced by an unnamed one, and vice versa.
    if answer["nearest"] and not answer["nearest"]["named"]:
        assert answer["same"] is False
        if answer["nearest_named"]:
            assert answer["nearest_named"]["named"] is True
            assert answer["nearest_named"]["hop"] >= answer["nearest"]["hop"]
    if answer["nearest_named"]:
        assert answer["nearest_named"]["named"] is True


async def test_graph_exposes_the_traversal_with_real_values(client: httpx.AsyncClient) -> None:
    """The graph view must draw what the engine actually reached.

    Amounts and value shares cross the wire as decimal STRINGS: a smallest-unit
    sum routinely exceeds 2^53, and a JSON number would be silently rounded by
    the JavaScript that renders it.
    """
    start = await client.post(
        "/investigations",
        json={
            "chain": CHAIN,
            "address": ROOT,
            "objectives": ["find_prev_vasp", "find_next_vasp"],
        },
    )
    investigation_id = start.json()["investigation_id"]

    response = await client.get(f"/investigations/{investigation_id}/graph")
    assert response.status_code == 200
    body = response.json()
    assert body["chain"] == CHAIN
    assert body["root_address"] == ROOT

    nodes = {n["address"]: n for n in body["nodes"]}
    assert ROOT in nodes and nodes[ROOT]["hop"] == 0
    assert EXCHANGE_IN in nodes and EXCHANGE_OUT in nodes
    # Node identity includes direction, so each objective's trace is its own
    # path — a graph that merged them would draw one trace where there are two.
    assert nodes[EXCHANGE_IN]["direction"] == "backward"
    assert nodes[EXCHANGE_OUT]["direction"] == "forward"
    for node in body["nodes"]:
        assert node["value_share"] is None or isinstance(node["value_share"], str)

    assert body["edges"], "a traversal that reached two endpoints has edges"
    ids = {n["id"] for n in body["nodes"]}
    for edge in body["edges"]:
        # A dangling endpoint would draw a line to a node that is not on screen.
        assert edge["src"] in ids and edge["dst"] in ids
        if edge["kind"] == "movement":
            assert isinstance(edge["amount"], str)
            assert edge["asset_symbol"]
            assert isinstance(edge["asset_verified"], bool)

    assert body["node_total"] == len(body["nodes"])
    assert body["truncated"] is False


async def test_graph_says_when_it_is_showing_less_than_it_found(
    client: httpx.AsyncClient,
) -> None:
    """A bounded view must never read as complete coverage."""
    start = await client.post(
        "/investigations",
        json={
            "chain": CHAIN,
            "address": ROOT,
            "objectives": ["find_prev_vasp", "find_next_vasp"],
        },
    )
    investigation_id = start.json()["investigation_id"]

    full = (await client.get(f"/investigations/{investigation_id}/graph")).json()
    assert full["node_total"] > 1, "fixture needs more than one node to bound"

    capped = (await client.get(f"/investigations/{investigation_id}/graph?limit=1")).json()
    assert len(capped["nodes"]) == 1
    assert capped["node_total"] == full["node_total"]
    assert capped["truncated"] is True
    # Bounding keeps the NEAREST node, matching the engine's own claim order,
    # rather than an arbitrary page.
    assert capped["nodes"][0]["hop"] == 0
    # Edges to nodes that were cut are cut too, never left dangling.
    assert capped["edges"] == []


async def test_graph_budget_is_spent_per_hop_not_nearest_first(
    client: httpx.AsyncClient,
) -> None:
    """A bounded graph must keep its DEPTH, not just its nearest addresses.

    Measured on a live trace reaching hops -2..+2: a flat nearest-first cap of
    120 was consumed entirely by hops -1..+1 and silently dropped all 202 nodes
    at hop 2, so the picture lost a dimension without saying so. Capping each
    (hop, direction) group separately keeps every hop that was reached.
    """
    start = await client.post(
        "/investigations",
        json={
            "chain": CHAIN,
            "address": ROOT,
            "objectives": ["find_prev_vasp", "find_next_vasp"],
        },
    )
    investigation_id = start.json()["investigation_id"]

    full = (await client.get(f"/investigations/{investigation_id}/graph")).json()
    hops_reached = {n["hop"] for n in full["nodes"]}
    assert hops_reached == {0, 1, 2}, "fixture must reach depth to prove anything"

    # Tight per-level cap, generous overall: every hop survives.
    tight = (await client.get(f"/investigations/{investigation_id}/graph?per_level=1")).json()
    assert {n["hop"] for n in tight["nodes"]} == hops_reached
    seen: set[tuple[int, str | None]] = set()
    for node in tight["nodes"]:
        key = (node["hop"], node["direction"])
        assert key not in seen, "per_level=1 must return one node per hop and direction"
        seen.add(key)

    # An overall cap takes the best-ranked node of each group before the second
    # of any group, so it thins every hop rather than deleting the far ones.
    thin = (await client.get(f"/investigations/{investigation_id}/graph?limit=3")).json()
    assert len(thin["nodes"]) == 3
    assert thin["node_total"] == full["node_total"]
    assert thin["truncated"] is True
    groups = [(n["hop"], n["direction"]) for n in thin["nodes"]]
    assert len(set(groups)) == len(groups), "the overall cap must spread across groups"


async def test_graph_missing_investigation_is_404(client: httpx.AsyncClient) -> None:
    missing = "00000000-0000-0000-0000-000000000000"
    assert (await client.get(f"/investigations/{missing}/graph")).status_code == 404


async def test_unknown_chain_is_rejected(client: httpx.AsyncClient) -> None:
    response = await client.post(
        "/investigations",
        json={"chain": "atlantis", "address": "x", "objectives": ["find_prev_vasp"]},
    )
    assert response.status_code == 422


async def test_unrecognized_address_is_rejected_when_chain_omitted(
    client: httpx.AsyncClient,
) -> None:
    response = await client.post(
        "/investigations",
        json={"address": "definitely-not-an-address", "objectives": ["find_prev_vasp"]},
    )
    assert response.status_code == 422
    detail = response.json()["detail"]
    assert detail["reason"] == "unrecognized"
    assert detail["candidates"] == []
    assert "recognize" in detail["message"].lower()


async def test_missing_investigation_is_404(client: httpx.AsyncClient) -> None:
    missing = "00000000-0000-0000-0000-000000000000"
    assert (await client.get(f"/investigations/{missing}")).status_code == 404
    assert (await client.get(f"/investigations/{missing}/findings")).status_code == 404


async def test_objectives_required(client: httpx.AsyncClient) -> None:
    response = await client.post(
        "/investigations",
        json={"chain": CHAIN, "address": ROOT, "objectives": []},
    )
    assert response.status_code == 422  # pydantic min_length


async def test_chain_is_resolved_without_the_caller_specifying_it(
    client: httpx.AsyncClient,
) -> None:
    """An investigator types an address, not a ledger name.

    The synthetic chain's format is unambiguous, so this exercises the
    single-candidate path end to end: no `chain` in the request, and the
    response echoes what was traced.
    """
    response = await client.post(
        "/investigations",
        json={"address": ROOT, "objectives": ["find_prev_vasp"]},
    )
    assert response.status_code == 201, response.text
    assert response.json()["chain"] == CHAIN


async def test_a_failed_probe_never_reports_the_chain_as_empty() -> None:
    """An outage must not become a claim about the blockchain.

    If probing raises, the honest answer is "could not check" — reporting
    "no transactions found" would state a fact about someone's money on the
    strength of a cache being down.
    """
    from fastapi import HTTPException

    from cipherchain.api.app import _resolve_chain
    from cipherchain.chains.base import ChainRegistry
    from tests.investigation.conftest import FakeAdapter

    class BrokenAdapter(FakeAdapter):
        async def address_history(self, address, **kwargs):  # type: ignore[no-untyped-def]
            raise RuntimeError("provider down")

    registry = ChainRegistry()
    registry.register(BrokenAdapter(chain="alpha"))
    registry.register(BrokenAdapter(chain="beta"))

    with pytest.raises(HTTPException) as caught:
        await _resolve_chain(registry, ROOT)
    detail = caught.value.detail
    assert detail["reason"] == "ambiguous"
    assert "Could not check" in detail["message"]
    assert set(detail["candidates"]) == {"alpha", "beta"}


async def test_two_genuinely_active_chains_is_a_hard_stop() -> None:
    """The "never guess a ledger" invariant, pinned.

    Evidence may narrow the field to one candidate — that is elimination, not a
    guess. It must never pick BETWEEN two live candidates. Disclosing such a
    choice in a log line does not make the answer less wrong: a confident trace
    of the wrong chain is a confident claim about the wrong money.
    """
    from fastapi import HTTPException

    from cipherchain.api.app import _resolve_chain
    from cipherchain.chains.base import ChainRegistry
    from tests.investigation.conftest import FakeAdapter

    registry = ChainRegistry()
    registry.register(FakeAdapter(chain="alpha"))  # both serve the default ledger,
    registry.register(FakeAdapter(chain="beta"))  # so both are genuinely active

    with pytest.raises(HTTPException) as caught:
        await _resolve_chain(registry, ROOT)
    detail = caught.value.detail
    assert detail["reason"] == "ambiguous"
    assert set(detail["candidates"]) == {"alpha", "beta"}


async def test_one_active_chain_resolves_even_when_others_match_the_format() -> None:
    """The UX win: other chains matched the FORMAT but hold no history, so the
    single chain that does is the only candidate the evidence supports."""
    from cipherchain.api.app import _resolve_chain
    from cipherchain.chains.base import ChainRegistry
    from tests.investigation.conftest import FakeAdapter

    registry = ChainRegistry()
    registry.register(FakeAdapter(chain="alpha"))
    registry.register(FakeAdapter(chain="beta", ledger=()))  # recognises, holds nothing

    assert await _resolve_chain(registry, ROOT) == "alpha"


async def test_one_active_chain_plus_an_unreadable_one_still_stops() -> None:
    """One live candidate plus a chain we could not read is not one candidate —
    the unread chain may hold history too."""
    from fastapi import HTTPException

    from cipherchain.api.app import _resolve_chain
    from cipherchain.chains.base import ChainRegistry
    from tests.investigation.conftest import FakeAdapter

    class BrokenAdapter(FakeAdapter):
        async def address_history(self, address, **kwargs):  # type: ignore[no-untyped-def]
            raise RuntimeError("provider down")

    registry = ChainRegistry()
    registry.register(FakeAdapter(chain="alpha"))
    registry.register(BrokenAdapter(chain="beta"))

    with pytest.raises(HTTPException) as caught:
        await _resolve_chain(registry, ROOT)
    assert caught.value.detail["reason"] == "ambiguous"
    assert set(caught.value.detail["candidates"]) == {"alpha", "beta"}


@pytest.mark.parametrize("address", ["", "   ", "\t\n"])
async def test_a_blank_address_is_a_422_not_a_500(client, address: str) -> None:
    """A caller who forgot a field must not be told the server broke.

    Left to the domain this was a 500: `Address` raises ValueError on an empty
    value and nothing catches it. Probed live against the running API.
    """
    response = await client.post(
        "/investigations",
        json={"address": address, "chain": "testchain", "objectives": ["find_prev_vasp"]},
    )
    assert response.status_code == 422


async def test_a_pasted_address_keeps_working_with_surrounding_whitespace(client) -> None:
    """Stripping, not just rejecting: the chain detector already tolerates it,
    so the API must not be stricter than the thing it feeds."""
    response = await client.post(
        "/investigations",
        json={
            "address": f"  {ROOT}  ",
            "chain": CHAIN,
            "objectives": ["find_prev_vasp"],
        },
    )
    assert response.status_code == 201


async def test_a_lost_feed_reaches_the_status_body_by_name(
    client: httpx.AsyncClient, session: AsyncSession
) -> None:
    """The degradation has to be visible to a caller, not just to a log file.

    Once the keyed provider quotas are spent, feeds go missing as a matter of
    course — that is the design. What makes it safe is that a consumer can see
    WHICH feed went missing: a run whose token feed died reports the same
    ``completed`` status as a clean one, and a caller tracing a stablecoin
    payment cannot tell those apart from the status string alone.
    """
    start = await client.post(
        "/investigations",
        json={"chain": CHAIN, "address": ROOT, "objectives": ["find_prev_vasp"]},
    )
    investigation_id = start.json()["investigation_id"]

    clean = (await client.get(f"/investigations/{investigation_id}")).json()["coverage"]
    assert clean["addresses_missing_feeds"] == 0
    assert clean["feeds_unavailable"] == []

    investigations = InvestigationRepository(session)
    node = (await investigations.graph_nodes(uuid.UUID(investigation_id)))[0]
    code = FeedGap(chain=CHAIN, capability=Capability.TOKEN_TRANSFERS).code
    await investigations.mark_feed_unavailable(node.id, code)
    await session.commit()

    degraded = (await client.get(f"/investigations/{investigation_id}")).json()["coverage"]
    assert degraded["addresses_missing_feeds"] == 1
    assert degraded["feeds_unavailable"] == [code]
    assert degraded["complete"] is False, "a run that lost a feed must not report complete"


async def test_a_caller_can_turn_the_pursuit_off_and_the_record_says_so(
    client: httpx.AsyncClient,
) -> None:
    """Pursuit is on by default, and the two knobs that govern it are on the wire.

    A caller who needs a predictable spend — a scheduled sweep, a demo, anything
    metered — has to be able to say so, and the run has to be readable as the
    one they asked for afterwards. Both fields therefore round-trip into
    ``budgets`` on the status body, which is the record of what was authorised.
    """
    started = await client.post(
        "/investigations",
        json={
            "chain": CHAIN,
            "address": ROOT,
            "objectives": ["find_prev_vasp", "find_next_vasp"],
            "budgets": {"api_calls": 1, "pursue_until_answered": False, "max_extensions": 2},
        },
    )
    assert started.status_code == 201, started.text
    assert started.json()["status"] == "partial", "with pursuit off, one call stops the run"

    status = (await client.get(f"/investigations/{started.json()['investigation_id']}")).json()
    assert status["budgets"]["pursue_until_answered"] is False
    assert status["budgets"]["max_extensions"] == 2
    assert status["coverage"]["budget_extensions"] == []


async def test_a_pursued_run_reports_what_it_granted_itself(client: httpx.AsyncClient) -> None:
    """The default run buys its own allowance, and the wire says how much.

    ``budgets`` keeps stating what the caller asked for, so without this a
    consumer comparing budget to spend would read a run that cost three times
    its allowance as an accounting bug rather than as the pursuit it was.
    """
    started = await client.post(
        "/investigations",
        json={
            "chain": CHAIN,
            "address": ROOT,
            "objectives": ["find_prev_vasp", "find_next_vasp"],
            "budgets": {"api_calls": 1},
        },
    )
    investigation_id = started.json()["investigation_id"]

    status = (await client.get(f"/investigations/{investigation_id}")).json()
    assert status["budgets"]["api_calls"] == 1
    assert status["spent"]["api_calls"] == 3
    extensions = status["coverage"]["budget_extensions"]
    assert len(extensions) == 2
    assert extensions[0].startswith("budget 'api_calls' extended from 1 to 2")

    findings = (await client.get(f"/investigations/{investigation_id}/findings")).json()
    named = {f["direction"] for f in findings["findings"] if f["kind"] == "vasp_endpoint"}
    assert named == {"backward", "forward"}, "the pursuit is what reached both exchanges"
