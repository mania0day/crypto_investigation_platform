"""Resuming a run that stopped on a budget (RFC step 6).

A partial result is the normal outcome of a real trace: budgets exist so that a
runaway expansion cannot spend a day of provider quota, and the run stops with
its frontier checkpointed in ``nodes``. Without this route the only way forward
was to start again from the subject address — refetching everything, re-deriving
everything, and producing a SECOND investigation record for the same question.

The tests below lock the three things that make the route safe rather than
merely convenient: it re-enters the existing traversal instead of restarting it,
it refuses a run whose question was already answered, and it refuses a budget
that would stop it again immediately.
"""

from __future__ import annotations

import uuid

import httpx
import pytest
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from cipherchain.chains.base import ChainRegistry
from cipherchain.investigation import InvestigationEngine
from cipherchain.storage.repositories import InvestigationRepository
from tests.investigation.conftest import CHAIN, EXCHANGE_IN, ROOT, FakeAdapter

# One API call buys exactly one address expansion, so the run stops with the
# root expanded and its counterparties sitting on the frontier, unread.
#
# Pursuit is switched OFF here, and only here, because these tests are about the
# route that exists for a run which stopped: with it on the engine grants itself
# another allowance rather than handing back a partial result, which is the
# whole point of the default and would leave this fixture with nothing to
# resume. The route still matters — pursuit has a ceiling, and past it a human
# with a bigger budget is again the only way forward.
STOPS_EARLY = {
    "api_calls": 1,
    "seconds": 300,
    "max_depth": 6,
    "max_nodes": 500,
    "pursue_until_answered": False,
}
ROOMY = {"api_calls": 100, "seconds": 300, "max_depth": 6, "max_nodes": 500}


def body(**overrides: object) -> dict[str, object]:
    return {
        "chain": CHAIN,
        "address": ROOT,
        "objectives": ["find_prev_vasp", "find_next_vasp"],
        **overrides,
    }


@pytest.fixture
async def partial_run(client: httpx.AsyncClient) -> str:
    started = await client.post("/investigations", json=body(budgets=STOPS_EARLY))
    assert started.status_code == 201, started.text
    assert started.json()["status"] == "partial", "fixture must stop on a budget"
    return str(started.json()["investigation_id"])


def adapter_of(engine: InvestigationEngine) -> FakeAdapter:
    registry: ChainRegistry = engine.registry
    adapter = registry.get(CHAIN)
    assert isinstance(adapter, FakeAdapter)
    return adapter


async def test_a_partial_run_picks_up_where_it_stopped(
    client: httpx.AsyncClient, partial_run: str, investigation_engine: InvestigationEngine
) -> None:
    """The frontier was checkpointed, so this is a continuation, not a restart.

    Two claims, and the second is the one worth the fixture: the run finishes
    (finding the endpoint it had not reached yet), and the root address is never
    fetched a second time. A "resume" that re-expanded already-expanded nodes
    would spend the fresh budget re-deriving what the record already holds and
    would look identical from the outside.
    """
    adapter = adapter_of(investigation_engine)
    fetched_before = list(adapter.history_calls)
    assert fetched_before.count(ROOT) == 1

    resumed = await client.post(f"/investigations/{partial_run}/resume", json={"budgets": ROOMY})

    assert resumed.status_code == 200, resumed.text
    assert resumed.json()["status"] == "completed"
    assert adapter.history_calls.count(ROOT) == 1, "the root was re-expanded on resume"
    assert len(adapter.history_calls) > len(fetched_before), "no new address was expanded"

    findings = (await client.get(f"/investigations/{partial_run}/findings")).json()
    endpoints = {
        f["subject_address"] for f in findings["findings"] if f["kind"] == "vasp_endpoint"
    }
    assert EXCHANGE_IN in endpoints, "the resumed run never reached the endpoint"


async def test_resuming_carries_the_earlier_spend_forward(
    client: httpx.AsyncClient, partial_run: str
) -> None:
    """A resume must not hand the run a fresh allowance of everything.

    The engine seeds the tracker from what the row already spent, so the new
    budget is a NEW CEILING, not a new wallet. If it were a wallet, an
    investigator could walk past a configured cap by resuming in a loop, and the
    cap is what protects a provider quota that is shared with every other case.
    """
    before = (await client.get(f"/investigations/{partial_run}")).json()
    assert before["spent"]["api_calls"] == 1

    await client.post(f"/investigations/{partial_run}/resume", json={"budgets": ROOMY})

    after = (await client.get(f"/investigations/{partial_run}")).json()
    assert after["spent"]["api_calls"] > before["spent"]["api_calls"]
    assert after["budgets"]["api_calls"] == ROOMY["api_calls"], "the new budget was not stored"


async def test_a_completed_run_cannot_be_resumed(client: httpx.AsyncClient) -> None:
    """The dangerous one. A completed run answered its objectives and filed its
    terminals; re-running it would append a second, contradictory ending to the
    same record — a report showing one investigation stopping twice.

    409, and the record is untouched: no second run started behind the refusal.
    """
    started = await client.post("/investigations", json=body(budgets=ROOMY))
    investigation_id = started.json()["investigation_id"]
    assert started.json()["status"] == "completed"
    before = (await client.get(f"/investigations/{investigation_id}/findings")).json()

    refused = await client.post(
        f"/investigations/{investigation_id}/resume", json={"budgets": ROOMY}
    )

    assert refused.status_code == 409
    assert "completed" in refused.json()["detail"]
    after = (await client.get(f"/investigations/{investigation_id}/findings")).json()
    assert after["status"] == "completed"
    assert len(after["findings"]) == len(before["findings"])


async def test_a_run_that_is_already_going_cannot_be_resumed_underneath_itself(
    client: httpx.AsyncClient,
    sessions: async_sessionmaker[AsyncSession],
    partial_run: str,
) -> None:
    """Two loops over one frontier would fetch the same addresses twice, charge
    both to the budget and file both sets of findings — ``claim_frontier``
    selects without locking, so nothing downstream would notice.

    The status guard is the concurrency control, and it lives in the UPDATE that
    claims the run, so a second caller arriving mid-flight is refused rather
    than admitted by a stale read.
    """
    async with sessions() as session:
        await InvestigationRepository(session).set_status(uuid.UUID(partial_run), "running")
        await session.commit()

    refused = await client.post(f"/investigations/{partial_run}/resume", json={"budgets": ROOMY})

    assert refused.status_code == 409
    assert "already running" in refused.json()["detail"]


async def test_a_budget_that_is_already_spent_is_refused_with_the_number_to_beat(
    client: httpx.AsyncClient, partial_run: str
) -> None:
    """Resuming on a spent budget is a no-op wearing the clothes of progress.

    The engine carries prior spend forward, so a resume on the SAME budget
    exhausts on its first check and writes a second partial result identical to
    the first: the API reports 200 and the investigation moved nowhere. It is
    refused instead, and the refusal says what to raise and above what.
    """
    refused = await client.post(
        f"/investigations/{partial_run}/resume", json={"budgets": STOPS_EARLY}
    )

    assert refused.status_code == 422
    detail = refused.json()["detail"]
    assert detail["reason"] == "budget_already_spent"
    assert "api_calls" in detail["message"]
    assert detail["spent"]["api_calls"] == 1

    still = (await client.get(f"/investigations/{partial_run}")).json()
    assert still["status"] == "partial", "a refused resume must not have started anything"


async def test_resuming_with_no_body_uses_the_default_budgets(
    client: httpx.AsyncClient, partial_run: str
) -> None:
    """``POST .../resume`` with nothing in it is legal — budgets are the only
    thing a resume can carry, and the defaults are a sane fresh allowance."""
    resumed = await client.post(f"/investigations/{partial_run}/resume", json={})

    assert resumed.status_code == 200, resumed.text
    assert resumed.json()["status"] == "completed"


async def test_resuming_an_unknown_investigation_is_404(client: httpx.AsyncClient) -> None:
    missing = "00000000-0000-0000-0000-000000000000"
    response = await client.post(f"/investigations/{missing}/resume", json={"budgets": ROOMY})
    assert response.status_code == 404


async def test_a_resume_cannot_change_the_question(
    client: httpx.AsyncClient, partial_run: str
) -> None:
    """Only budgets are accepted. A trace that quietly changed its subject,
    chain or objectives halfway would be uncitable: the record would name one
    question and the findings would answer another."""
    resumed = await client.post(
        f"/investigations/{partial_run}/resume",
        json={"budgets": ROOMY, "address": "someone_else", "objectives": ["find_next_vasp"]},
    )

    assert resumed.status_code == 200, resumed.text
    body_out = resumed.json()
    assert body_out["root_address"] == ROOT
    assert set(body_out["objectives"]) == {"find_prev_vasp", "find_next_vasp"}
