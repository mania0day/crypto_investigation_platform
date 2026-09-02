"""Every route, against every kind of caller.

``test_auth.py`` proves the dependency works. This file proves it is actually
MOUNTED — on each route, with the right scope — which is the part that fails
silently. A guard that was never attached looks exactly like a guard that
passed, and the endpoints behind these paths start expensive traces and serve
case material naming victims and suspects.

So the table below is exhaustive on purpose, and the four callers are the four
that exist: nobody, somebody with the wrong scope, somebody with the right
scope, and somebody whose key has been revoked.
"""

from __future__ import annotations

import logging
import uuid
from pathlib import Path

import httpx
import pytest
from fastapi import FastAPI
from fastapi.dependencies.models import Dependant
from fastapi.routing import APIRoute
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from cipherchain.api.app import create_app
from cipherchain.api.auth import ApiKeyAuth, Scope, revoke_key
from cipherchain.investigation import InvestigationEngine
from tests.api.conftest import bearer, client_for, mint
from tests.investigation.conftest import CHAIN, ROOT

START_BODY = {"chain": CHAIN, "address": ROOT, "objectives": ["find_prev_vasp"]}

# (method, path template, required scope, body). One row per guarded route: a
# route added without a row is a route nobody proved is guarded.
GUARDED = [
    ("GET", "/investigations/{id}", Scope.READ, None),
    ("GET", "/investigations/{id}/findings", Scope.READ, None),
    ("GET", "/investigations/{id}/graph", Scope.READ, None),
    ("GET", "/investigations/{id}/report", Scope.READ, None),
    ("POST", "/investigations", Scope.INVESTIGATE, START_BODY),
    ("POST", "/investigations/{id}/resume", Scope.INVESTIGATE, {}),
]

OTHER_SCOPE = {Scope.READ: Scope.INVESTIGATE, Scope.INVESTIGATE: Scope.READ}


async def call(
    client: httpx.AsyncClient,
    method: str,
    path: str,
    body: dict[str, object] | None,
    investigation_id: str,
) -> httpx.Response:
    return await client.request(
        method, path.format(id=investigation_id), json=body if method == "POST" else None
    )


@pytest.fixture
async def started(client: httpx.AsyncClient) -> str:
    """One real, finished investigation, so a permitted call is a 2xx not a 404."""
    response = await client.post("/investigations", json=START_BODY)
    assert response.status_code == 201, response.text
    return str(response.json()["investigation_id"])


@pytest.mark.parametrize(("method", "path", "scope", "body"), GUARDED)
async def test_a_guarded_route_refuses_a_caller_with_no_key(
    app: FastAPI, method: str, path: str, scope: Scope, body: dict[str, object] | None
) -> None:
    """401, with the scheme named and nothing else said."""
    async with client_for(app) as anonymous:
        response = await call(anonymous, method, path, body, str(uuid.uuid4()))

    assert response.status_code == 401, f"{method} {path} is not guarded"
    assert response.json()["detail"] == "invalid or missing API key"
    assert response.headers["WWW-Authenticate"] == "Bearer"


@pytest.mark.parametrize(("method", "path", "scope", "body"), GUARDED)
async def test_a_guarded_route_refuses_the_wrong_scope(
    app: FastAPI,
    sessions: async_sessionmaker[AsyncSession],
    method: str,
    path: str,
    scope: Scope,
    body: dict[str, object] | None,
) -> None:
    """403: the key is known, it simply may not do this.

    Scopes are flat — READ does not imply INVESTIGATE and INVESTIGATE does not
    imply READ — so each route is probed with exactly the other one. A key that
    starts traces must not be able to read case material it was not issued for,
    and the read-only key a dashboard carries must not be able to spend an
    afternoon of provider quota.
    """
    key = await mint(sessions, OTHER_SCOPE[scope])
    async with client_for(app, key.token) as wrong:
        response = await call(wrong, method, path, body, str(uuid.uuid4()))

    assert response.status_code == 403, f"{method} {path} accepted the wrong scope"
    assert str(scope) in response.json()["detail"]


@pytest.mark.parametrize(("method", "path", "scope", "body"), GUARDED)
async def test_a_guarded_route_admits_the_right_scope(
    app: FastAPI,
    sessions: async_sessionmaker[AsyncSession],
    started: str,
    method: str,
    path: str,
    scope: Scope,
    body: dict[str, object] | None,
) -> None:
    """The guard is a gate, not a wall: the right key gets through it.

    What the route then ANSWERS is each route's own business (resume, for one,
    refuses a completed investigation with a 409) — this only asserts the
    request was never turned away at the door.
    """
    key = await mint(sessions, scope)
    async with client_for(app, key.token) as permitted:
        response = await call(permitted, method, path, body, started)

    assert response.status_code not in (401, 403), response.text


@pytest.mark.parametrize(("method", "path", "scope", "body"), GUARDED)
async def test_a_guarded_route_refuses_a_revoked_key(
    app: FastAPI,
    sessions: async_sessionmaker[AsyncSession],
    started: str,
    method: str,
    path: str,
    scope: Scope,
    body: dict[str, object] | None,
) -> None:
    """Revocation is the emergency lever and it has to reach every route.

    A key revoked while a session is open must stop working on the very next
    request — there is no verification cache anywhere, and this is the test that
    forbids one being added per route later.
    """
    key = await mint(sessions, scope)
    async with client_for(app, key.token) as caller:
        assert (await call(caller, method, path, body, started)).status_code not in (401, 403)

        async with sessions() as session:
            await revoke_key(session, key.key_id)

        response = await call(caller, method, path, body, started)

    assert response.status_code == 401, f"{method} {path} kept honouring a revoked key"


async def test_no_route_is_mounted_without_a_guard(
    sessions: async_sessionmaker[AsyncSession],
    investigation_engine: InvestigationEngine,
    tmp_path: Path,
) -> None:
    """The table above cannot catch a route nobody added a row for. This can.

    Read off the ROUTE TABLE, not the OpenAPI document. The schema was the
    obvious source and it has a hole exactly the shape of the mistake this test
    exists to catch: ``include_in_schema=False`` removes an endpoint from the
    document while leaving it mounted and serving, so a route added that way —
    the demo page already is one — would be invisible to a schema-derived check
    however carefully it was written. Walking ``app.routes`` and the real
    dependency tree of each one sees every route FastAPI will actually answer.

    The exceptions are named individually and are the whole open surface:
    liveness, a static page carrying no case material, and the three endpoints
    FastAPI mounts for itself. Those three are asserted separately below —
    ``APIRoute`` does not cover them, which is its own way of hiding a route,
    so the schema and docs pages are checked as routes rather than assumed.
    """
    (tmp_path / "index.html").write_text("<html><body>demo</body></html>", encoding="utf-8")
    auth = ApiKeyAuth(sessions, enabled=True)
    app = create_app(
        sessions,
        investigation_engine,
        run_in_background=False,
        pool_metrics=lambda: {},
        static_dir=tmp_path,
        auth=auth,
    )
    # Every dependency this guard hands out, whatever scope it was asked for.
    guards = {id(dependency) for dependency in auth._dependencies.values()}

    def is_guarded(dependant: Dependant) -> bool:
        if dependant.call is not None and id(dependant.call) in guards:
            return True
        return any(is_guarded(sub) for sub in dependant.dependencies)

    unguarded = {
        f"{method} {route.path}"
        for route in app.routes
        if isinstance(route, APIRoute)
        for method in sorted(route.methods or ())
        if not is_guarded(route.dependant)
    }

    assert unguarded == {"GET /healthz", "GET /"}


async def test_the_framework_owned_routes_are_the_only_other_open_thing(
    sessions: async_sessionmaker[AsyncSession],
    investigation_engine: InvestigationEngine,
) -> None:
    """FastAPI mounts three endpoints of its own, and they answer anonymously.

    They are NOT ``APIRoute``, so the guard sweep above cannot see them — a
    route table walked with an ``isinstance`` filter has a blind spot exactly
    the width of "not the class I expected", and this is what sits in it.

    They are left open deliberately: the schema and the two documentation pages
    describe the API's SHAPE and carry no case material, and ``/docs`` is how
    the demo is driven. What matters is that the decision is written down and
    checked, so a deployment that wants the route table private knows it must
    pass ``openapi_url=None`` — and so that a future route hiding behind the
    same class blind spot fails this assertion instead of shipping.
    """
    app = create_app(
        sessions,
        investigation_engine,
        run_in_background=False,
        auth=ApiKeyAuth(sessions, enabled=True),
    )
    framework_routes = {
        f"{method} {route.path}"
        for route in app.routes
        if not isinstance(route, APIRoute)
        for method in sorted(getattr(route, "methods", None) or ())
        if method != "HEAD"
    }
    assert framework_routes == {
        "GET /openapi.json",
        "GET /docs",
        "GET /docs/oauth2-redirect",
        "GET /redoc",
    }

    async with client_for(app) as anonymous:
        answered = {path: (await anonymous.get(path)).status_code for path in ("/openapi.json",)}
        schema = (await anonymous.get("/openapi.json")).json()

    assert answered["/openapi.json"] == 200, "the schema route is documented as open"
    # And what it publishes is the shape, not an investigation: no path in it
    # can be called without a key (proved exhaustively above), so an anonymous
    # reader learns which routes exist and nothing about any case.
    assert set(schema["paths"]) >= {"/investigations", "/healthz"}


# ──────────────────────────────── the open surface ────────────────────────────────


async def test_healthz_needs_no_key(app: FastAPI) -> None:
    """Liveness is asked before anybody has a key, and reveals nothing."""
    async with client_for(app) as anonymous:
        response = await anonymous.get("/healthz")

    assert response.status_code == 200
    assert response.json() == {"status": "ok"}


async def test_turning_auth_off_opens_the_real_routes_and_says_so(
    sessions: async_sessionmaker[AsyncSession],
    investigation_engine: InvestigationEngine,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """The local-dev escape hatch, exercised on the real app rather than a stub.

    ``AUTH_ENABLED=false`` has to actually work — an operator debugging on
    loopback should not have to mint a key — and it has to be impossible to do
    accidentally. The banner is the compensating control, and it is only worth
    anything if it fires on the app people really run.
    """
    with caplog.at_level(logging.WARNING, logger="cipherchain.api.auth"):
        app = create_app(
            sessions,
            investigation_engine,
            run_in_background=False,
            auth=ApiKeyAuth(sessions, enabled=False),
        )

    async with client_for(app) as anonymous:
        response = await anonymous.get(f"/investigations/{uuid.uuid4()}")

    assert response.status_code == 404, "the route ran; it simply had nothing to return"
    assert "DISABLED" in " ".join(record.message for record in caplog.records)


async def test_metrics_is_read_scoped(sessions: async_sessionmaker[AsyncSession]) -> None:
    """Provider health is an operational map of this deployment.

    It names the vendors the host holds credentials for and how much quota is
    left, which is reconnaissance for anyone deciding when a trace is cheapest
    to disrupt. It is a read, and it is behind ``read``.
    """
    app = create_app(
        sessions,
        engine=None,
        engine_provider=lambda: pytest.fail("metrics must not touch the engine"),
        run_in_background=False,
        pool_metrics=lambda: {"providers": {}},
        auth=ApiKeyAuth(sessions, enabled=True),
    )
    key = await mint(sessions, Scope.READ)

    async with client_for(app) as anonymous:
        assert (await anonymous.get("/metrics")).status_code == 401
    async with client_for(app, key.token) as reader:
        assert (await reader.get("/metrics")).status_code == 200


# ──────────────────────────────── the demo page ────────────────────────────────


def demo_app(
    sessions: async_sessionmaker[AsyncSession],
    engine: InvestigationEngine,
    static_dir: Path,
    *,
    demo_api_key: str | None = None,
) -> FastAPI:
    (static_dir / "index.html").write_text(
        "<html><head><title>CipherChain</title></head><body>demo</body></html>", encoding="utf-8"
    )
    return create_app(
        sessions,
        engine,
        run_in_background=False,
        static_dir=static_dir,
        auth=ApiKeyAuth(sessions, enabled=True),
        demo_api_key=demo_api_key,
    )


async def test_the_demo_page_itself_is_public(
    sessions: async_sessionmaker[AsyncSession],
    investigation_engine: InvestigationEngine,
    tmp_path: Path,
) -> None:
    """A static page holding no case material is not worth a key.

    Everything it then calls is authenticated like any other client's request,
    which is the property that matters — a login screen on the HTML would guard
    nothing.
    """
    async with client_for(demo_app(sessions, investigation_engine, tmp_path)) as anonymous:
        response = await anonymous.get("/")

    assert response.status_code == 200
    assert "demo" in response.text
    assert "CIPHERCHAIN_API_KEY" not in response.text


async def test_the_demo_page_carries_the_key_the_demo_script_minted(
    sessions: async_sessionmaker[AsyncSession],
    investigation_engine: InvestigationEngine,
    tmp_path: Path,
) -> None:
    """scripts/demo.sh mints a dev key and the page is served holding it.

    The bundled UI has no field to ask an operator for a key, so with auth on —
    and auth is ON by default — the demo would 401 on every action. The choice
    made was to hand the page a real, scoped, revocable key rather than to turn
    the guard off for the demo, because "it works with auth disabled" is the one
    demo that proves nothing about the deployed system.
    """
    key = await mint(sessions, Scope.READ, Scope.INVESTIGATE, label="demo")
    app = demo_app(sessions, investigation_engine, tmp_path, demo_api_key=key.token)

    async with client_for(app) as anonymous:
        page = await anonymous.get("/")
        # The page is served with the key; the API still checks it like any other.
        guarded = await anonymous.get(f"/investigations/{uuid.uuid4()}")
        allowed = await anonymous.get(
            f"/investigations/{uuid.uuid4()}", headers=bearer(key.token)
        )

    assert key.token in page.text
    assert "window.CIPHERCHAIN_API_KEY" in page.text
    assert page.text.index("CIPHERCHAIN_API_KEY") < page.text.index("</head>")
    assert guarded.status_code == 401
    assert allowed.status_code == 404  # past the guard, no such investigation


async def test_a_token_that_is_not_key_shaped_is_never_written_into_the_page(
    sessions: async_sessionmaker[AsyncSession],
    investigation_engine: InvestigationEngine,
    tmp_path: Path,
) -> None:
    """The injection refuses rather than escapes.

    A token is written into a ``<script>`` element on a page served without
    authentication. Anything that is not token-shaped — including something
    carrying markup — is dropped and logged, so there is one rule with one
    answer instead of an escaping judgement renewed every time the shim changes.
    """
    app = demo_app(
        sessions,
        investigation_engine,
        tmp_path,
        demo_api_key='cc_1.x"</script><script>alert(1)</script>',
    )

    async with client_for(app) as anonymous:
        page = await anonymous.get("/")

    assert page.status_code == 200
    assert "alert(1)" not in page.text
    assert "CIPHERCHAIN_API_KEY" not in page.text


async def test_the_demo_page_is_never_served_from_a_stale_browser_cache(
    sessions: async_sessionmaker[AsyncSession],
    investigation_engine: InvestigationEngine,
    tmp_path: Path,
) -> None:
    """An edit to the bundled UI has to be one refresh away, for everyone.

    The route re-reads the file per request, which makes the SERVER always
    current — but that is only half of it. Served with no caching directive at
    all, a browser may reuse the document from its own cache without asking
    again, and the page then keeps showing a previous build to whoever already
    had it while a colleague on a cold cache sees the new one. That is a real
    failure this project hit: two people on the same commit, looking at
    different UIs, with nothing in the server logs to show for it.

    The directive also differs by path on purpose, so both are asserted here.
    """
    key = await mint(sessions, Scope.READ, Scope.INVESTIGATE, label="demo")
    with_key = demo_app(sessions, investigation_engine, tmp_path, demo_api_key=key.token)
    without_key = demo_app(sessions, investigation_engine, tmp_path)

    async with client_for(with_key) as anonymous:
        embedded = await anonymous.get("/")
    async with client_for(without_key) as anonymous:
        plain = await anonymous.get("/")

    # Carrying a live credential: never written to the disk cache at all.
    assert embedded.headers["cache-control"] == "no-store"
    assert key.token in embedded.text

    # No credential, so revalidating is enough — and a 304 makes it cheap.
    assert plain.headers["cache-control"] == "no-cache"


def test_the_bundled_ui_is_found_in_the_layout_this_repo_actually_uses() -> None:
    """A 404 at "/" is the one deployment failure that looks like success.

    ``STATIC_DIR`` was a hardcoded ``parents[4] / "frontend"``, correct only when
    the backend is the root of its own repository. Vendored into the CipherChain
    repo it sits one level down with the page at ``backend/static/``, so a fresh
    clone booted, answered healthz, enforced auth — and served no UI. The route
    is only registered when the file exists, so its absence is indistinguishable
    from a deliberately headless deployment, and the dashboard's Investigate
    button lands on nothing.
    """
    from cipherchain.api.app import STATIC_DIR

    assert (STATIC_DIR / "index.html").is_file(), (
        f"the bundled UI was not found; STATIC_DIR resolved to {STATIC_DIR}"
    )
