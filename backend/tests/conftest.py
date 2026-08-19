"""Storage integration tests run against a real Postgres 16 container.

Session setup: start a throwaway container, wait for readiness, apply the
Alembic migrations (which tests the migrations themselves), yield the URL.
Per-test: truncate every model table so tests are independent.

Set CIPHERCHAIN_TEST_DATABASE_URL to reuse an external Postgres instead;
tests skip cleanly when neither docker nor an external URL is available.
"""

from __future__ import annotations

import os
import shutil
import socket
import subprocess
import sys
import time
import uuid
from collections.abc import AsyncIterator, Iterator
from pathlib import Path

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker

from cipherchain.storage.db import create_engine, create_session_factory
from cipherchain.storage.tables import Base

BACKEND_DIR = Path(__file__).resolve().parents[1]


def _free_port() -> int:
    """A port the OS says is free right now.

    This was a fixed 54329, which made two concurrent pytest sessions
    impossible: container names were already unique, but the host port binding
    was not, so the second `docker run -p 127.0.0.1:54329:5432` died with "port
    is already allocated" and `check=True` turned that into a session error.
    The failure reads like a broken test suite rather than a busy port, which is
    the expensive part — it sends you looking in the wrong place.
    """
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
        probe.bind(("127.0.0.1", 0))
        return int(probe.getsockname()[1])


def _pg_url(port: int) -> str:
    return f"postgresql+asyncpg://cipherchain:cipherchain@127.0.0.1:{port}/cipherchain_test"


def _docker_ok() -> bool:
    if shutil.which("docker") is None:
        return False
    return subprocess.run(["docker", "info"], capture_output=True).returncode == 0


def _wait_ready(container: str, timeout: float = 90.0) -> None:
    deadline = time.time() + timeout
    consecutive = 0
    while time.time() < deadline:
        probe = subprocess.run(
            [
                "docker",
                "exec",
                container,
                "pg_isready",
                "-U",
                "cipherchain",
                "-d",
                "cipherchain_test",
            ],
            capture_output=True,
        )
        # postgres containers restart once during init: require two
        # consecutive successful probes before declaring readiness.
        consecutive = consecutive + 1 if probe.returncode == 0 else 0
        if consecutive >= 2:
            return
        time.sleep(0.5)
    raise RuntimeError("postgres container did not become ready in time")


def _migrate(url: str) -> None:
    subprocess.run(
        [sys.executable, "-m", "alembic", "upgrade", "head"],
        cwd=BACKEND_DIR,
        env={**os.environ, "DATABASE_URL": url},
        check=True,
        capture_output=True,
    )


@pytest.fixture(scope="session")
def pg_url() -> Iterator[str]:
    external = os.environ.get("CIPHERCHAIN_TEST_DATABASE_URL")
    if external:
        _migrate(external)
        yield external
        return
    if not _docker_ok():
        pytest.skip("storage integration tests need docker or CIPHERCHAIN_TEST_DATABASE_URL")
    name = f"cipherchain-test-pg-{uuid.uuid4().hex[:8]}"
    # _free_port only reports a port that was free a moment ago; another session
    # can claim it in between. Retry rather than fail the whole run on a race
    # that resolves itself.
    for attempt in range(5):
        port = _free_port()
        started = subprocess.run(
            [
                "docker",
                "run",
                "-d",
                "--rm",
                "--name",
                name,
                "-e",
                "POSTGRES_USER=cipherchain",
                "-e",
                "POSTGRES_PASSWORD=cipherchain",
                "-e",
                "POSTGRES_DB=cipherchain_test",
                "-p",
                f"127.0.0.1:{port}:5432",
                "postgres:16-alpine",
            ],
            capture_output=True,
        )
        if started.returncode == 0:
            break
        if attempt == 4:
            raise RuntimeError(
                f"could not start test postgres after 5 attempts: "
                f"{started.stderr.decode(errors='replace')[:300]}"
            )
        time.sleep(0.5)
    url = _pg_url(port)
    try:
        _wait_ready(name)
        _migrate(url)
        yield url
    finally:
        subprocess.run(["docker", "stop", name], capture_output=True)


@pytest.fixture
async def engine(pg_url: str) -> AsyncIterator[AsyncEngine]:
    engine = create_engine(pg_url)
    async with engine.begin() as conn:
        tables = ", ".join(f'"{t.name}"' for t in reversed(Base.metadata.sorted_tables))
        await conn.execute(text(f"TRUNCATE {tables} RESTART IDENTITY CASCADE"))
    yield engine
    await engine.dispose()


@pytest.fixture
async def session(engine: AsyncEngine) -> AsyncIterator[AsyncSession]:
    factory = create_session_factory(engine)
    async with factory() as s:
        yield s


@pytest.fixture
async def sessions(engine: AsyncEngine) -> AsyncIterator[async_sessionmaker[AsyncSession]]:
    """Session factory — for components (like the engine) that open their own
    sessions per checkpoint. Shared across every test package."""
    yield create_session_factory(engine)
