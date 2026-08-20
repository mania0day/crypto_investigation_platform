"""The lead route and the graph field it feeds.

Two things are being pinned. First, that an investigator can ask for names and
see them on the graph. Second — and this is the one that matters — that asking
changes the picture and not the answer: the same run's report and findings must
be byte-identical before and after, because a pending claim is not evidence.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime

import httpx
import pytest
from fastapi import FastAPI
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from cipherchain.api.auth import Scope
from cipherchain.core.models import (
    Address,
    Direction,
    Evidence,
    EvidenceKind,
    Finding,
    FindingKind,
)
from cipherchain.storage.repositories import (
    FactRepository,
    InvestigationRepository,
    LabelRepository,
)
from tests.api.conftest import client_for, mint

T0 = datetime(2026, 8, 20, 12, 0, tzinfo=UTC)
TRON_ENDPOINT = "TU4vEruvZwLLkSfV9bNw12EJTPvNr7Pvaa"


async def a_tron_trace(sessions: async_sessionmaker[AsyncSession]) -> uuid.UUID:
    """A run holding one behaviourally-identified, unnamed Tron endpoint."""
    async with sessions() as session:
        facts = FactRepository(session)
        repo = InvestigationRepository(session)
        root_id = await facts.get_or_create_address(Address("tron", "Troot"))
        row = await repo.create(
            root_address_id=root_id,
            objectives=["find_prev_vasp"],
            budgets={"api_calls": 10, "seconds": 60, "max_depth": 2, "max_nodes": 50},
            engine_version="0.1.0",
            ruleset_version="2026-08-16",
        )
        address_id = await facts.get_or_create_address(Address("tron", TRON_ENDPOINT))
        await repo.add_address_node(
            row.id,
            address_id,
            direction=Direction.BACKWARD,
            hop_distance=1,
            value_share=1_000,
            discovered_reason="find_prev_vasp",
        )
        await repo.add_finding(
            row.id,
            Finding(
                kind=FindingKind.VASP_ENDPOINT,
                subject=Address(chain="tron", value=TRON_ENDPOINT),
                summary="service endpoint (operator unnamed): collects from 144 …",
                confidence=0.75,
                evidence=(
                    Evidence(
                        kind=EvidenceKind.HEURISTIC_INFERENCE,
                        summary="bidirectional counterparty degree exceeds thresholds",
                        heuristic="service-endpoint@1",
                        confidence=0.75,
                    ),
                ),
                direction=Direction.BACKWARD,
            ),
            subject_address_id=address_id,
        )
        await session.commit()
        return row.id


async def a_pending_name(sessions: async_sessionmaker[AsyncSession], entity: str) -> None:
    async with sessions() as session:
        await LabelRepository(session).upsert_claim(
            chain="tron",
            address=TRON_ENDPOINT,
            entity=entity,
            category="vasp",
            role="unknown",
            confidence=0.3,
            status="pending",
            method="community",
            source="tronscan-public-tag",
            retrieved_at=T0,
        )
        await session.commit()


async def test_unknown_investigation_is_404(client: httpx.AsyncClient) -> None:
    response = await client.post(f"/investigations/{uuid.uuid4()}/leads")
    assert response.status_code == 404


async def test_chain_without_a_reader_is_stated_not_started(
    client: httpx.AsyncClient, sessions: async_sessionmaker[AsyncSession]
) -> None:
    """An Ethereum trace gets an explanation, not a silent zero."""
    async with sessions() as session:
        facts = FactRepository(session)
        repo = InvestigationRepository(session)
        root_id = await facts.get_or_create_address(Address("ethereum", "0xroot"))
        row = await repo.create(
            root_address_id=root_id,
            objectives=["find_prev_vasp"],
            budgets={"api_calls": 10, "seconds": 60, "max_depth": 2, "max_nodes": 50},
            engine_version="0.1.0",
            ruleset_version="2026-08-16",
        )
        address_id = await facts.get_or_create_address(Address("ethereum", "0xendpoint"))
        await repo.add_address_node(
            row.id,
            address_id,
            direction=Direction.BACKWARD,
            hop_distance=1,
            value_share=1,
            discovered_reason="find_prev_vasp",
        )
        await repo.add_finding(
            row.id,
            Finding(
                kind=FindingKind.VASP_ENDPOINT,
                subject=Address(chain="ethereum", value="0xendpoint"),
                summary="service endpoint (operator unnamed): …",
                confidence=0.7,
                evidence=(
                    Evidence(
                        kind=EvidenceKind.HEURISTIC_INFERENCE,
                        summary="degree exceeds thresholds",
                        heuristic="service-endpoint@1",
                        confidence=0.7,
                    ),
                ),
                direction=Direction.BACKWARD,
            ),
            subject_address_id=address_id,
        )
        await session.commit()

    response = await client.post(f"/investigations/{row.id}/leads")
    assert response.status_code == 202
    body = response.json()
    assert body["started"] is False
    assert body["unsupported_chains"] == ["ethereum"]


async def test_read_only_key_cannot_spend_someone_elses_bandwidth(
    app: FastAPI, sessions: async_sessionmaker[AsyncSession]
) -> None:
    """Same separation as /harvest/run: reading is not permission to fetch."""
    investigation_id = await a_tron_trace(sessions)
    key = await mint(sessions, Scope.READ, label="reader")
    async with client_for(app, key.token) as reader:
        response = await reader.post(f"/investigations/{investigation_id}/leads")
    assert response.status_code == 403


async def test_graph_carries_the_name_as_an_unverified_tag(
    client: httpx.AsyncClient, sessions: async_sessionmaker[AsyncSession]
) -> None:
    investigation_id = await a_tron_trace(sessions)

    before = await client.get(f"/investigations/{investigation_id}/graph")
    node = next(n for n in before.json()["nodes"] if n["address"] == TRON_ENDPOINT)
    assert node["unverified_tags"] == []

    await a_pending_name(sessions, "Bybit")

    after = await client.get(f"/investigations/{investigation_id}/graph")
    node = next(n for n in after.json()["nodes"] if n["address"] == TRON_ENDPOINT)
    assert node["unverified_tags"] == [
        {"entity": "Bybit", "source": "tronscan-public-tag", "confidence": 0.3}
    ]


async def test_a_name_changes_the_picture_and_not_the_answer(
    client: httpx.AsyncClient, sessions: async_sessionmaker[AsyncSession]
) -> None:
    """The whole point, asserted directly.

    An investigator gains a lead on the canvas. The findings — what the report
    is built from and what anyone would cite — do not move at all.
    """
    investigation_id = await a_tron_trace(sessions)
    before = (await client.get(f"/investigations/{investigation_id}/findings")).json()

    await a_pending_name(sessions, "Bybit")

    after = (await client.get(f"/investigations/{investigation_id}/findings")).json()
    assert after == before

    graph = (await client.get(f"/investigations/{investigation_id}/graph")).json()
    node = next(n for n in graph["nodes"] if n["address"] == TRON_ENDPOINT)
    assert node["unverified_tags"][0]["entity"] == "Bybit"


@pytest.mark.parametrize("entity", ["Bybit", "MXC"])
async def test_pending_names_never_enter_the_attributor_load(
    sessions: async_sessionmaker[AsyncSession], entity: str
) -> None:
    await a_tron_trace(sessions)
    await a_pending_name(sessions, entity)
    async with sessions() as session:
        active = await LabelRepository(session).active_labels()
    assert [row for row in active if row.source == "tronscan-public-tag"] == []
