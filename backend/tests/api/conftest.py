"""Fixtures for the API tests: a real engine, a real guard, a real key.

The app under test is built the way production builds it — auth ENABLED — and
the default client carries a minted key. That is deliberate: with the guard
switched off in the fixture, every route test would prove the route works for
an unauthenticated caller and nothing would notice the day a guard fell off.
Here the smoke tests pass through the real dependency on every request, and
``test_route_auth.py`` covers what happens without a key, with the wrong scope,
and with a revoked one.

Runs are synchronous (``run_in_background=False``) so that POST returns after
the investigation has finished and a test can assert on its result rather than
poll for it.
"""

from __future__ import annotations

from collections.abc import AsyncIterator, Iterator, Sequence

import httpx
import pytest
from fastapi import FastAPI
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from cipherchain.api.app import create_app
from cipherchain.api.auth import ApiKeyAuth, MintedKey, Scope, mint_key
from cipherchain.chains.base import ChainRegistry
from cipherchain.investigation import InvestigationEngine
from tests.investigation.conftest import (
    EXCHANGE_IN,
    EXCHANGE_OUT,
    FakeAdapter,
    MapAttributor,
)

VASP_LABELS = {
    EXCHANGE_IN: ("Test Exchange In", "vasp"),
    EXCHANGE_OUT: ("Test Exchange Out", "vasp"),
}


def bearer(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


async def mint(
    sessions: async_sessionmaker[AsyncSession],
    *scopes: Scope,
    label: str | None = None,
) -> MintedKey:
    """A key with the given scopes. No scopes named means both."""
    wanted: Sequence[Scope] = scopes or (Scope.READ, Scope.INVESTIGATE)
    async with sessions() as session:
        return await mint_key(session, wanted, label=label)


def client_for(app: FastAPI, token: str | None = None) -> httpx.AsyncClient:
    return httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app),
        base_url="http://test",
        headers=bearer(token) if token else {},
    )


@pytest.fixture
def investigation_engine(
    sessions: async_sessionmaker[AsyncSession],
) -> InvestigationEngine:
    registry = ChainRegistry()
    registry.register(FakeAdapter())
    return InvestigationEngine(registry, sessions, MapAttributor(VASP_LABELS))


@pytest.fixture
def app(
    sessions: async_sessionmaker[AsyncSession], investigation_engine: InvestigationEngine
) -> Iterator[FastAPI]:
    yield create_app(
        sessions,
        investigation_engine,
        run_in_background=False,
        auth=ApiKeyAuth(sessions, enabled=True),
    )


@pytest.fixture
async def client(
    app: FastAPI, sessions: async_sessionmaker[AsyncSession]
) -> AsyncIterator[httpx.AsyncClient]:
    """The everyday caller: one key, both scopes, sent on every request."""
    key = await mint(sessions, Scope.READ, Scope.INVESTIGATE, label="api tests")
    async with client_for(app, key.token) as authenticated:
        yield authenticated
