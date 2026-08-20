"""FastAPI application factory and routes.

Thin edge over the engine: start an investigation, inspect its status, read
its findings, print its report, resume it when a budget cut it short.
Investigations run as background tasks (vision: long investigations are
background jobs); a run can be requested synchronously for tests and scripted
use via ``run_in_background=False``.

Every route that touches an investigation is behind an API key
(``cipherchain.api.auth``): starting a trace spends provider quota and minutes of
work, and everything else here serves case material that names victims and
suspects. The factory builds the guard from settings when the caller does not
pass one, so a wiring that forgets about auth gets it switched ON rather than
off — the other default fails silently, which for this system means publishing
an investigation console and looking fine while doing it.

The open surface is four things and is meant to be read as a list, because
"everything is guarded" is the kind of claim that stops being true quietly:
``/healthz`` (liveness, asked before anybody has a key), ``/`` (the bundled
static page), and FastAPI's own ``/openapi.json`` + ``/docs`` + ``/redoc``,
which publish the SHAPE of the API and no data from it. A deployment that does
not want its route table public passes ``openapi_url=None``; the demo does,
since the docs page is how an operator drives it. ``tests/api/test_route_auth.py``
asserts that exact set against the live route table, so a fifth entry is a test
failure rather than a discovery.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import re
import tempfile
import uuid
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import asynccontextmanager
from functools import lru_cache
from pathlib import Path
from typing import Any, cast

import httpx
from fastapi import Depends, FastAPI, HTTPException, Query, Request, Response
from fastapi.responses import FileResponse, HTMLResponse
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker

from cipherchain.analysis.assets import AssetPolicy, build_asset_policy
from cipherchain.api.auth import ApiKeyAuth, AuthenticatedKey, Scope
from cipherchain.api.schemas import (
    AnswerOut,
    CoverageOut,
    FindingOut,
    FindingsResponse,
    GraphEdgeOut,
    GraphNodeOut,
    GraphResponse,
    InvestigationStatusResponse,
    ResumeInvestigationRequest,
    StartInvestigationRequest,
    StartInvestigationResponse,
    SyncStatusResponse,
    UnverifiedTagOut,
)
from cipherchain.chains.base import ChainRegistry
from cipherchain.core.config import Settings, get_settings
from cipherchain.core.errors import UnknownChain
from cipherchain.core.logging import configure_logging
from cipherchain.core.models import Address
from cipherchain.harvest.runs import CycleAlreadyRunning, start_cycle, sync_status
from cipherchain.harvest.scheduler import DEFAULT_DROP_DIR
from cipherchain.intel.attributor_source import build_store_attributor
from cipherchain.intel.leads import SUPPORTED_CHAINS, enrich_investigation
from cipherchain.investigation.answers import RankedFinding, select_answers
from cipherchain.investigation.budgets import Budgets
from cipherchain.investigation.engine import InvestigationEngine
from cipherchain.investigation.objectives import Objective
from cipherchain.reporting import (
    ChromiumNotFound,
    PdfRenderError,
    ReportNotFound,
    collect_coverage,
    collect_report,
    default_vasp_lookup,
    render_html,
    render_pdf,
)
from cipherchain.runtime import build_chain_registry, build_engine, build_provider_pool
from cipherchain.storage.db import create_engine, create_session_factory
from cipherchain.storage.provider_cache import PostgresProviderCache
from cipherchain.storage.repositories import (
    FactRepository,
    InvestigationRepository,
    LabelRepository,
)

logger = logging.getLogger(__name__)

# Investigations with a lead lookup in flight, mapped to the task running it.
# In-process and deliberately so: this guards politeness toward a third-party
# API, not the correctness of any record, and a second worker doing one extra
# pass costs 22 requests rather than a wrong answer. A durable lock would be a
# lifecycle to maintain for no invariant it protects.
#
# Holding the Task — not just the id — is load-bearing: the event loop keeps
# only a weak reference to a bare create_task, so a fire-and-forget lookup can
# be garbage-collected mid-flight and simply stop, leaving no names and no
# error (ruff RUF006).
_leads_in_flight: dict[uuid.UUID, asyncio.Task[None]] = {}

RunLauncher = Callable[[uuid.UUID], Awaitable[None]]

# How many nodes one graph read returns by default, and the ceiling a caller
# may ask for. A real theft trace reaches four figures of addresses; drawing
# all of them is unreadable, so the view is bounded and SAYS it is bounded.
#
# The per-level cap is the one that matters. Spending the whole budget nearest
# -first hands it all to the first hop the moment that hop fans out wide, and
# the picture silently loses its depth: on a live trace reaching hops -2..+2, a
# flat 120 returned hops -1..+1 and dropped every one of the 202 nodes at hop 2.
GRAPH_NODE_LIMIT = 240
GRAPH_PER_LEVEL = 20
# The ceiling is a payload bound, not an opinion about how much is worth
# drawing — a caller asking for everything (an export, an offline analysis)
# should get everything the trace holds. At 1000 it was neither: investigation
# ba0783b9 holds ~1600 address nodes, so the largest graph anyone could ask for
# came back missing a third of the trace with only ``truncated`` to say so.
# 2500 clears that trace with headroom and still bounds one response: measured
# against the models in schemas.py, a node serializes to ~360 bytes of JSON and
# an edge to ~315, which puts the ceiling near 1.8 MB — and every node inside it
# is one the receiving renderer has to lay out.
GRAPH_NODE_MAX = 2500


@lru_cache(maxsize=1)
def _asset_policy() -> AssetPolicy:
    """The same verified-asset pack the engine founds its heuristics on.

    Read here so the graph can report which amounts rest on an asset whose
    provenance is established. Built from files only — no provider is touched,
    so the presentation layer stays outside the provider-access invariant.
    """
    return build_asset_policy()


def _find_static_dir() -> Path:
    """Where the single-file demo UI lives, across both repo layouts.

    This was a hardcoded ``parents[4] / "frontend"``, which is correct only when
    the backend sits at the root of its own repository. Vendored into the
    CipherChain repo the backend is one level down and the UI moved to
    ``backend/static/``, so "/" answered 404 — a fresh clone booted, passed
    healthz, enforced auth, and served no page. Nothing failed loudly: the route
    is only registered when the file is found, so its absence looks exactly like
    a deployment that meant to run headless.

    Both candidates are checked rather than one being chosen, so the same code
    works standalone and vendored, and neither layout silently loses its UI.
    """
    here = Path(__file__).resolve()
    for candidate in (here.parents[3] / "static", here.parents[4] / "frontend"):
        if (candidate / "index.html").is_file():
            return candidate
    # Nothing found: return the vendored location so the caller's own
    # file-existence guard reports a missing page against a real path.
    return here.parents[3] / "static"


STATIC_DIR = _find_static_dir()

# Set by scripts/demo.sh to the token it just minted. Nothing else should set
# it: it makes the UI page carry a working credential (see create_app).
DEMO_KEY_ENV = "CIPHERCHAIN_DEMO_API_KEY"

# Strong references to in-flight background investigations. Without this the
# event loop holds only weak refs and a run can be garbage-collected mid-flight
# (REVIEW_FINDINGS.md #10).
_BACKGROUND_RUNS: set[asyncio.Task[None]] = set()

# Chain auto-resolution. One provider call per candidate chain: fetch a small
# page and compare how much history each chain actually holds.
_PROBE_LIMIT = 25

# Report formats the /report route will serve, and the content type each is
# asked for by. ``?format=`` wins; Accept decides when it is absent.
_REPORT_FORMATS = ("html", "pdf")
_PDF_MEDIA_TYPE = "application/pdf"

# Only a run that stopped on a BUDGET may be resumed. "completed" means the
# frontier ran dry and the objectives were answered; resuming it would re-file
# terminals for a closed question. "failed" means the engine raised, which needs
# diagnosis rather than another lap. Enforced again in the repository, where the
# same guard is the concurrency control.
RESUMABLE_STATUS = "partial"

# A demo token is pasted into a page, so it must not be able to close the script
# element it is pasted into. Real tokens are "cc_<hex>.<urlsafe-b64>"; anything
# else is refused rather than escaped, because escaping invites a second opinion
# about what is safe and this check has only one.
_DEMO_TOKEN_SHAPE = re.compile(r"[A-Za-z0-9_.\-]{16,256}")


def create_app(
    session_factory: async_sessionmaker[AsyncSession],
    engine: InvestigationEngine | None = None,
    *,
    engine_provider: Callable[[], InvestigationEngine] | None = None,
    run_in_background: bool = True,
    pool_metrics: Callable[[], dict[str, object]] | None = None,
    lifespan: Any = None,
    static_dir: Path | None = None,
    auth: ApiKeyAuth | None = None,
    demo_api_key: str | None = None,
    harvest_drop_dir: Path | None = None,
) -> FastAPI:
    """Build the API.

    Routes are declared exactly once, here. The engine may be supplied
    directly (tests) or resolved per request through ``engine_provider``
    (production, where it is built inside the lifespan around an HTTP
    client). Mounting routes at startup instead would double-mount on a
    second lifespan cycle — see REVIEW_FINDINGS.md.

    ``auth`` defaults to whatever settings say, which is ON. A test or a
    tool that wants the routes open has to say so by passing
    ``ApiKeyAuth(sessions, enabled=False)`` — there is no way to end up
    unauthenticated by omission.

    ``demo_api_key`` is the token ``scripts/demo.sh`` mints for its own local
    run. When set, the bundled single-file UI is served with it embedded, since
    that page has no way to ask an operator for a key. It is opt-in, announced
    in the log at startup, and it makes anything that can GET ``/`` able to act
    as that key — which is why demo.sh binds the loopback interface and nothing
    else should set it.
    """
    if engine is None and engine_provider is None:
        raise ValueError("create_app needs either engine or engine_provider")
    resolve = (
        engine_provider
        if engine_provider is not None
        else lambda: cast(InvestigationEngine, engine)
    )
    # Fail-closed default. Building the guard here rather than making it a
    # required argument keeps every existing caller working; defaulting it to
    # None-means-open would have kept them working too, and silently.
    guard = auth if auth is not None else ApiKeyAuth.from_settings(session_factory)
    # Resolved from the same env var the scheduler reads, so the panel reports
    # on the directory the cycle actually loads from. Pointing them at different
    # places would make the panel say "no drop" while a drop sits ingested.
    drops = (
        harvest_drop_dir
        if harvest_drop_dir is not None
        else Path(os.environ.get("CIPHERCHAIN_DROP_DIR", DEFAULT_DROP_DIR))
    )
    # Resolved from this module's location rather than the working directory:
    # the API is started from several places (demo.sh, start.sh, systemd) and a
    # relative path would make the Sync-now button work from some of them.
    harvest_script = Path(__file__).resolve().parents[3] / "scripts" / "harvest.sh"
    # Bound to names first: a bare ``Depends(...)`` call in a default argument
    # is what ruff's B008 objects to, and the name reads the same to FastAPI.
    # NEVER write ``Annotated[AuthenticatedKey, Depends(guard.requires(...))]``
    # here — under ``from __future__ import annotations`` that annotation is a
    # string evaluated against module globals, where the local ``guard`` does
    # not exist, and FastAPI silently reads the parameter as a query parameter
    # instead of a guard (see ApiKeyAuth's docstring).
    investigator = Depends(guard.requires(Scope.INVESTIGATE))
    reader = Depends(guard.requires(Scope.READ))

    app = FastAPI(
        title="CipherChain",
        version="0.1.0",
        summary="Blockchain investigation engine",
        lifespan=lifespan,
    )

    async def launch(started_engine: InvestigationEngine, investigation_id: uuid.UUID) -> None:
        if run_in_background:
            # Detach: the HTTP response returns immediately (vision:
            # investigations are long-running background jobs).
            task = asyncio.create_task(_safe_run(started_engine, investigation_id))
            _BACKGROUND_RUNS.add(task)  # keep a strong ref; GC could collect it
            task.add_done_callback(_BACKGROUND_RUNS.discard)
        else:
            await started_engine.run(investigation_id)

    @app.get("/healthz")
    async def healthz() -> dict[str, str]:
        """Liveness only, and deliberately open.

        It answers "is this process up", which a load balancer and a bare
        ``curl`` both need to ask before anybody has a key. It reveals nothing
        about any investigation — every route that does is guarded.
        """
        return {"status": "ok"}

    @app.post("/investigations", response_model=StartInvestigationResponse, status_code=201)
    async def start_investigation(
        request: StartInvestigationRequest,
        key: AuthenticatedKey = investigator,
    ) -> StartInvestigationResponse:
        # Resolve once: resolving again after start() persisted the row could
        # 503 mid-request and orphan an investigation nothing will ever run.
        active = resolve()
        chain = request.chain or await _resolve_chain(active.registry, request.address)
        try:
            investigation_id = await active.start(
                chain,
                request.address,
                request.objectives,
                request.budgets.to_budgets(),
            )
        except UnknownChain as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc
        # Attribution, not decoration: "who started this trace" is the first
        # question asked of a result that turns out to be wrong, and the key id
        # is the only durable answer (the label lives with the key, not here).
        logger.info("investigation %s started by key %s", investigation_id, key.key_id)
        await launch(active, investigation_id)
        async with session_factory() as session:
            row = await InvestigationRepository(session).get(investigation_id)
        status = row.status if row else "created"
        return StartInvestigationResponse(
            investigation_id=investigation_id, status=status, chain=chain
        )

    @app.post(
        "/investigations/{investigation_id}/resume",
        response_model=InvestigationStatusResponse,
    )
    async def resume_investigation(
        investigation_id: uuid.UUID,
        request: ResumeInvestigationRequest,
        key: AuthenticatedKey = investigator,
    ) -> InvestigationStatusResponse:
        """Re-enter the loop on a run that stopped on a budget, not on an answer.

        The frontier is already checkpointed — every unexpanded node sits in
        ``nodes`` with ``state='frontier'`` — so this is not a restart. Nothing
        is refetched, nothing is re-derived, and the run picks up in the same
        claim order it left in.

        Guarded on ``partial`` because that is what "the budget ran out" looks
        like in the record. Resuming a COMPLETED run would be the dangerous one:
        it already filed "trace exhausted, no endpoint found" against every
        objective, and re-running it would append a second set of terminals to a
        question that was answered — a report showing two contradictory endings
        for one investigation.

        What a resume does NOT do is re-open what the earlier run closed. A
        branch that stopped at the depth horizon is ``terminal`` in the record,
        not ``frontier``, so raising ``max_depth`` here buys depth for addresses
        still waiting and nothing for the ones already reported as horizon
        stops. Reviving them would quietly rewrite an account the investigator
        has already read; tracing deeper than the first run's horizon is a new
        investigation.

        ``investigate`` scope, not ``read``: a resume spends exactly what a
        start spends.
        """
        # Resolved before anything is written, so a 503 from a not-yet-started
        # engine cannot leave the row marked 'running' with nothing running.
        active = resolve()
        budgets = request.budgets.to_budgets()
        async with session_factory() as session:
            investigations = InvestigationRepository(session)
            row = await investigations.get(investigation_id)
            if row is None:
                raise HTTPException(status_code=404, detail="investigation not found")
            if row.status != RESUMABLE_STATUS:
                raise HTTPException(status_code=409, detail=_not_resumable(row.status))
            reached = await investigations.count_nodes(investigation_id)
            _check_budgets_leave_room(budgets, spent=dict(row.spent), nodes=reached)
            if not await investigations.claim_for_resume(
                investigation_id, budgets=budgets.to_dict()
            ):
                # Lost the race: another resume claimed it between the read and
                # the update. One run, not two, over one frontier.
                raise HTTPException(status_code=409, detail=_not_resumable("running"))
            await session.commit()
        logger.info(
            "investigation %s resumed by key %s with budgets %s",
            investigation_id,
            key.key_id,
            budgets.to_dict(),
        )
        await launch(active, investigation_id)
        async with session_factory() as session:
            return await _status_response(session, investigation_id)

    @app.post("/harvest/run", status_code=202, dependencies=[investigator])
    async def harvest_run() -> dict[str, object]:
        """Start a harvest cycle now, without waiting for the timer.

        Guarded by INVESTIGATE, not READ. It spends bandwidth (a ~28 MB
        sanctions download) and writes labels, which is exactly the separation
        those two scopes exist for — the read-only key a reviewing analyst
        carries must not be able to start expensive work.

        202, not 200: the cycle takes minutes and this returns as soon as the
        child process exists. Watch it finish on the sync panel, which polls
        the same run row the cycle writes.

        409 when one is already in flight. Refused rather than queued — two
        concurrent reconciles over a half-written harvest can promote a label
        on evidence the other transaction has not committed.
        """
        async with session_factory() as session:
            try:
                pid = await start_cycle(session, script=harvest_script, drop_dir=drops)
            except CycleAlreadyRunning as exc:
                raise HTTPException(status_code=409, detail=str(exc)) from exc
            except OSError as exc:
                # The script is missing or not executable — a deployment fault,
                # not a client error, and worth saying precisely because the
                # button would otherwise look broken for no visible reason.
                raise HTTPException(
                    status_code=500, detail=f"could not start {harvest_script}: {exc}"
                ) from exc
        return {"started": True, "pid": pid}

    @app.get("/harvest/status", response_model=SyncStatusResponse, dependencies=[reader])
    async def harvest_status() -> SyncStatusResponse:
        """Is the label store being kept current — and if not, whose move is it?

        Guarded by READ rather than left open like ``/healthz``. It names the
        sources this deployment harvests, their publication dates and its label
        counts, which is a description of the operator's intelligence coverage
        and not a liveness check.

        Read-only against ``harvest_runs``; it never starts a cycle. Starting
        one from an HTTP request would put a 28 MB download and a full
        reconcile inside the API process, which is the arrangement
        ``scripts/harvest.sh`` argues against — the cycle belongs to a timer
        that owns its own process and exits.
        """
        async with session_factory() as session:
            return SyncStatusResponse.of(await sync_status(session, drop_dir=drops))

    @app.get(
        "/investigations/{investigation_id}",
        response_model=InvestigationStatusResponse,
        dependencies=[reader],
    )
    async def get_investigation(investigation_id: uuid.UUID) -> InvestigationStatusResponse:
        async with session_factory() as session:
            return await _status_response(session, investigation_id)

    @app.get(
        "/investigations/{investigation_id}/findings",
        response_model=FindingsResponse,
        dependencies=[reader],
    )
    async def get_findings(investigation_id: uuid.UUID) -> FindingsResponse:
        async with session_factory() as session:
            investigations = InvestigationRepository(session)
            row = await investigations.get(investigation_id)
            if row is None:
                raise HTTPException(status_code=404, detail="investigation not found")
            findings = await investigations.list_findings(investigation_id)
            ranked = await investigations.vasp_findings_with_hops(investigation_id)
            coverage = await collect_coverage(investigations, row)
        # Both answers, never one chosen quietly: "nearest" and "nearest named"
        # are different questions, and a run routinely answers them differently.
        answers = select_answers(
            [
                RankedFinding(
                    finding=r.finding,
                    hop=r.hop,
                    speculative=r.speculative,
                    speculative_basis=r.speculative_basis,
                )
                for r in ranked
            ],
            [Objective(o).direction for o in row.objectives],
        )
        return FindingsResponse(
            investigation_id=investigation_id,
            status=row.status,
            findings=[FindingOut.of(f) for f in findings],
            answers=[AnswerOut.of(a) for a in answers],
            coverage=CoverageOut.of(coverage),
        )

    @app.get(
        "/investigations/{investigation_id}/graph",
        response_model=GraphResponse,
        dependencies=[reader],
    )
    async def get_graph(
        investigation_id: uuid.UUID,
        limit: int = GRAPH_NODE_LIMIT,
        per_level: int = GRAPH_PER_LEVEL,
    ) -> GraphResponse:
        """The traversal graph: what was reached, from what, carrying what.

        Readable while a run is still going — the overlay is written as the
        engine expands, so polling this shows the trace growing.

        The caps are a rendering bound, not a claim about the trace: within each
        hop, nodes come back in the engine's own claim order (value share), and
        the total is returned alongside so the caller can state what it is not
        showing. ``per_level`` is what preserves depth — see graph_nodes().

        ``per_level`` binds ordinary nodes only. A labelled address, or one a
        VASP finding was filed against, comes back whatever its value share —
        otherwise the picture can omit the address the report's headline names,
        which is what it did on ba0783b9 — and it comes back with the nodes
        leading to it, since ``edges`` carries a line only when both its
        endpoints are among ``nodes``. So ``len(nodes)`` may exceed
        ``per_level`` times the number of levels; it is never more than
        ``limit``.
        """
        limit = max(1, min(limit, GRAPH_NODE_MAX))
        per_level = max(1, min(per_level, GRAPH_NODE_MAX))
        async with session_factory() as session:
            investigations = InvestigationRepository(session)
            row = await investigations.get(investigation_id)
            if row is None:
                raise HTTPException(status_code=404, detail="investigation not found")
            root = await FactRepository(session).get_address(row.root_address_id)
            nodes = await investigations.graph_nodes(
                investigation_id, limit=limit, per_level=per_level
            )
            total = await investigations.count_graph_nodes(investigation_id)
            edges = await investigations.graph_edges(
                investigation_id, node_ids=[n.id for n in nodes]
            )
            # One extra indexed read, scoped to the nodes actually being
            # drawn — not the whole label table, and not per node. Pending
            # rows only: this is the lead channel, and the query that feeds
            # the attributor is a different one that cannot see these.
            leads = await LabelRepository(session).pending_labels_for(
                root.chain if root else "", [n.address for n in nodes]
            )
        policy = _asset_policy()
        return GraphResponse(
            investigation_id=investigation_id,
            status=row.status,
            chain=root.chain if root else "",
            root_address=root.value if root else "",
            nodes=[
                GraphNodeOut.of(
                    n,
                    [
                        UnverifiedTagOut(
                            entity=lead.entity,
                            source=lead.source,
                            confidence=lead.confidence,
                        )
                        for lead in leads.get(n.address, ())
                    ],
                )
                for n in nodes
            ],
            edges=[
                GraphEdgeOut.of(
                    e,
                    asset_verified=(
                        e.asset_kind is not None
                        and policy.is_evidence_grade(
                            chain=root.chain if root else "",
                            kind=e.asset_kind,
                            contract=e.asset_contract,
                        )
                    ),
                )
                for e in edges
            ],
            node_total=total,
            # Compared against what was actually returned, never against the
            # caps: pinned nodes push ``len(nodes)`` above ``per_level`` times
            # the number of levels, so a formula derived from the caps would
            # call a graph truncated that is whole, or — worse — whole when the
            # pins were the only thing that fit.
            truncated=total > len(nodes),
        )

    @app.post(
        "/investigations/{investigation_id}/leads",
        status_code=202,
        dependencies=[investigator],
    )
    async def fetch_leads(investigation_id: uuid.UUID) -> dict[str, object]:
        """Ask a public explorer to NAME the endpoints behaviour identified.

        What this buys: on a Tron trace the walk can prove an address is
        exchange infrastructure and still name nobody, because one exchange
        publishes a signed Tron address list. This fills in names an explorer
        already shows — and files every one of them ``pending``, so they appear
        as leads on the graph and can never become the report's answer.

        INVESTIGATE, not READ, for the same reason as ``/harvest/run``: it
        spends outbound requests against a third party and writes label rows.

        202 because the lookups are deliberately serialised and spaced — a free
        public API asked politely takes about a second per address. The names
        land on the graph, which the dashboard already polls.

        409 while a pass is already in flight for this investigation. Two
        concurrent passes would double the request rate against the explorer
        for no extra names.
        """
        async with session_factory() as session:
            investigations = InvestigationRepository(session)
            if await investigations.get(investigation_id) is None:
                raise HTTPException(status_code=404, detail="investigation not found")
            candidates = await investigations.unnamed_service_endpoints(investigation_id)
        if investigation_id in _leads_in_flight:
            raise HTTPException(
                status_code=409, detail="a lead lookup is already running for this investigation"
            )
        supported = [a for chain, a in candidates if chain in SUPPORTED_CHAINS]
        skipped = sorted({chain for chain, _ in candidates if chain not in SUPPORTED_CHAINS})
        if not supported:
            # Not an error, and stated rather than returned as a silent zero:
            # "no explorer reader for this chain" and "the explorer knew
            # nobody" are different answers and must not look alike.
            return {
                "started": False,
                "candidates": 0,
                "unsupported_chains": skipped,
                "detail": "no endpoint on a chain with an explorer reader",
            }

        async def _run_leads() -> None:
            try:
                async with httpx.AsyncClient(timeout=25) as http:
                    await enrich_investigation(
                        investigation_id, session_factory=session_factory, http=http
                    )
            except Exception:  # pragma: no cover - defensive
                # A lead lookup that fails leaves the investigation exactly as
                # it was. It must never mark the run failed.
                logger.exception("lead lookup for %s failed", investigation_id)
            finally:
                _leads_in_flight.pop(investigation_id, None)

        _leads_in_flight[investigation_id] = asyncio.create_task(_run_leads())
        return {
            "started": True,
            "candidates": len(supported),
            "unsupported_chains": skipped,
        }

    @app.get(
        "/investigations/{investigation_id}/report",
        dependencies=[reader],
        # Declared by hand: the route returns a document, not a model, and
        # without this the docs page offers no way to see that a PDF is even
        # available — which is how a format nobody knows about goes unused.
        responses={
            200: {
                "description": "The investigation report.",
                "content": {"text/html": {}, _PDF_MEDIA_TYPE: {}},
            },
            503: {"description": "PDF asked for on a host with no headless browser."},
        },
    )
    async def get_report(
        investigation_id: uuid.UUID,
        http_request: Request,
        report_format: str | None = Query(
            default=None,
            alias="format",
            description="`html` (default) or `pdf`. Without it, the Accept header decides.",
        ),
    ) -> Response:
        """The document an investigator hands over, as HTML or as a PDF file.

        Format selection: ``?format=html|pdf`` wins outright; with no parameter,
        an ``Accept`` header asking for ``application/pdf`` gets the PDF and
        everything else gets HTML. Explicit beats negotiated because a link in
        an email cannot set headers, and a browser's Accept string is a
        wishlist, not an instruction.

        Both formats are printed from the SAME rendered HTML, in one request, so
        the file in the case record cannot say something different from the page
        the investigator read on screen.

        A missing browser is a 503 naming the paths it searched, never a
        zero-byte attachment: a PDF that opens blank reads as "the tool found
        nothing" instead of "the tool could not print", and the second is the
        truth. The HTML path does not depend on the browser at all and keeps
        working while the PDF path is broken.
        """
        wanted = _report_format(report_format, http_request.headers.get("accept"))
        async with session_factory() as session:
            try:
                # The lookup is bound to THIS session and is called while
                # collect_report runs, so it must stay inside the block.
                report = await collect_report(
                    session, investigation_id, vasp_lookup=default_vasp_lookup(session)
                )
            except ReportNotFound as exc:
                raise HTTPException(status_code=404, detail="investigation not found") from exc
        html = render_html(report)
        if wanted == "html":
            return HTMLResponse(html)
        return await _print_pdf(html, investigation_id)

    if pool_metrics is not None:

        @app.get("/metrics", dependencies=[reader])
        async def metrics() -> dict[str, object]:
            """Provider health and quota. Behind ``read`` like every other read.

            It names the vendors this deployment holds credentials for and how
            much of each quota is left — an operational map of the system, and
            free reconnaissance for anyone deciding when a trace is cheapest to
            disrupt. ``/healthz`` is the endpoint for "is it up".
            """
            return pool_metrics()

    # Guard on the FILE: a present-but-empty frontend dir would otherwise
    # register the route and then 500 on a missing index.html.
    index = static_dir / "index.html" if static_dir is not None else None
    if index is not None and index.is_file():
        embedded = _demo_token(demo_api_key)

        @app.get("/", include_in_schema=False)
        async def demo_ui() -> Response:
            """The bundled single-file UI.

            Served unauthenticated because it is a static document that contains
            no case material; every call it then makes is authenticated like any
            other client's.

            Re-read per request rather than cached at startup so editing the
            page is a refresh, not a restart.
            """
            if embedded is None:
                return FileResponse(index)
            return HTMLResponse(_with_demo_key(index.read_text(encoding="utf-8"), embedded))

    return app


async def _status_response(
    session: AsyncSession, investigation_id: uuid.UUID
) -> InvestigationStatusResponse:
    """One reader for "what is this run doing", used by status and by resume.

    Resume answers with the same shape rather than a bespoke body: the caller's
    next question is always "what are its budgets and what has it spent", which
    is exactly this, and a second nearly-identical model would drift from it.
    """
    investigations = InvestigationRepository(session)
    row = await investigations.get(investigation_id)
    if row is None:
        raise HTTPException(status_code=404, detail="investigation not found")
    root = await FactRepository(session).get_address(row.root_address_id)
    return InvestigationStatusResponse(
        investigation_id=row.id,
        status=row.status,
        chain=root.chain if root else "",
        root_address=root.value if root else "",
        objectives=list(row.objectives),
        budgets=dict(row.budgets),
        spent=dict(row.spent),
        engine_version=row.engine_version,
        ruleset_version=row.ruleset_version,
        error=row.error,
        # A handful of indexed COUNTs per poll, deliberately paid here rather
        # than left to a second endpoint. "completed" is the status of a run
        # that hit a page limit, a depth horizon and a supernode cap on the way
        # to draining its frontier, and this body was previously the whole of
        # what a client had to judge that by.
        coverage=CoverageOut.of(await collect_coverage(investigations, row)),
    )


def _not_resumable(status: str) -> str:
    """Why this run cannot be resumed, in the words a caller can act on."""
    if status == "completed":
        return (
            "this investigation completed — its frontier ran dry and its objectives were "
            "answered, so there is nothing to resume. Start a new investigation to trace "
            "further."
        )
    if status == "running":
        return "this investigation is already running; wait for it to stop before resuming"
    if status == "failed":
        return (
            "this investigation failed rather than running out of budget — see its error. "
            "Resuming would re-enter the loop that raised."
        )
    return (
        f"only an investigation that stopped on a budget can be resumed (status "
        f"'{RESUMABLE_STATUS}'); this one is '{status}'"
    )


def _check_budgets_leave_room(budgets: Budgets, *, spent: dict[str, Any], nodes: int) -> None:
    """Refuse a resume whose budget is already spent.

    The engine carries prior spend forward on purpose — a crash/resume cycle
    must not grant a fresh ``api_calls`` allowance and blow past the configured
    cap — so resuming on a budget the run has already consumed exhausts on the
    very first check and writes a second partial result identical to the first.
    That "resume" looks like progress in the API and is a no-op in the record,
    which is the worst of both. Better to say what to raise, and by how much.

    ``seconds`` is exempt: it is a per-run wall clock the tracker deliberately
    does not restore, so every resume gets it fresh.
    """
    already = _spent_int(spent, "api_calls")
    problems = []
    if budgets.api_calls <= already:
        problems.append(
            f"api_calls={budgets.api_calls} but {already} have already been spent — "
            f"raise it above {already}"
        )
    if budgets.max_nodes <= nodes:
        problems.append(
            f"max_nodes={budgets.max_nodes} but this investigation already holds {nodes} "
            f"nodes — raise it above {nodes}"
        )
    if problems:
        raise HTTPException(
            status_code=422,
            detail={
                "reason": "budget_already_spent",
                "message": (
                    "a resume on this budget would stop again immediately: " + "; ".join(problems)
                ),
                "spent": {"api_calls": already, "nodes": nodes},
            },
        )


def _spent_int(spent: dict[str, Any], key: str) -> int:
    """Spend is JSONB written by the engine; a junk value must not 500 a resume."""
    try:
        return int(spent.get(key, 0))
    except (TypeError, ValueError):
        return 0


def _report_format(requested: str | None, accept: str | None) -> str:
    """Decide html or pdf. Explicit parameter first, Accept header second."""
    if requested is not None:
        normalized = requested.strip().lower()
        if normalized not in _REPORT_FORMATS:
            raise HTTPException(
                status_code=422,
                detail=f"unknown report format '{requested}' — use one of "
                f"{', '.join(_REPORT_FORMATS)}",
            )
        return normalized
    return "pdf" if accept is not None and _PDF_MEDIA_TYPE in accept.lower() else "html"


async def _print_pdf(html: str, investigation_id: uuid.UUID) -> Response:
    """Print the rendered report and hand back the bytes, or raise.

    Rendering shells out to a browser, which is seconds of blocking work, so it
    runs off the event loop — a report print must not stall every other request
    on the process. The file is written into a temporary directory and read
    back rather than streamed from a path, so nothing outlives the response;
    ``render_pdf`` itself refuses to produce a file that is empty or headerless,
    and those refusals are surfaced here as errors with words in them.
    """
    with tempfile.TemporaryDirectory(prefix="cipherchain-report-") as workspace:
        target = Path(workspace) / f"cipherchain-{investigation_id}.pdf"
        try:
            payload = await asyncio.to_thread(_render_pdf_bytes, html, target)
        except ChromiumNotFound as exc:
            # 503, not 500: the service is fine, one capability is missing, and
            # the caller can have the same document as HTML right now.
            raise HTTPException(status_code=503, detail=str(exc)) from exc
        except PdfRenderError as exc:
            raise HTTPException(
                status_code=500,
                detail=f"the report could not be printed to PDF: {exc}",
            ) from exc
    return Response(
        content=payload,
        media_type=_PDF_MEDIA_TYPE,
        headers={
            "Content-Disposition": f'attachment; filename="cipherchain-{investigation_id}.pdf"',
        },
    )


def _render_pdf_bytes(html: str, target: Path) -> bytes:
    return render_pdf(html, target).read_bytes()


def _demo_token(token: str | None) -> str | None:
    """Accept a demo token for embedding, or refuse it and say so.

    Refuses rather than escapes anything that is not token-shaped. A token is
    about to be written into a ``<script>`` element on a page served without
    authentication; the shape check is one rule with one answer, where escaping
    would be a judgement call renewed every time the injection changes.
    """
    if not token:
        return None
    if not _DEMO_TOKEN_SHAPE.fullmatch(token):
        logger.error(
            "refusing to embed a demo API key in the UI: the token is not key-shaped "
            "(expected '<key-id>.<secret>'). The demo page will load unauthenticated."
        )
        return None
    logger.warning(
        "the demo UI at / is served with an embedded API key — anything that can reach "
        "this port can act as that key. Bind 127.0.0.1 only."
    )
    return token


def _with_demo_key(html: str, token: str) -> str:
    """Hand the bundled UI a key it has no field to ask for.

    Publishes the token as ``window.CIPHERCHAIN_API_KEY`` and stops there. The page's
    own ``api()`` helper reads it and sets the header itself, so the demo makes
    exactly the request any other client makes. An earlier version wrapped
    ``window.fetch`` to attach the header behind the page's back; that worked,
    but it meant the demo would keep working if the UI ever stopped sending a
    key of its own — the one deployment where the omission is invisible, and
    every other deployment broken.

    ``json.dumps`` rather than a bare quote: it escapes the token for a JS
    string literal. The token is shape-checked in :func:`_demo_token` before it
    reaches here, so this is the second of two independent reasons it cannot
    close the element it sits in.
    """
    shim = "<script>\nwindow.CIPHERCHAIN_API_KEY = " + json.dumps(token) + ";\n</script>"
    # Before </head> so the constant exists before any of the page's own script
    # runs; prepended if the page has no head, which still beats every call it
    # makes.
    if "</head>" in html:
        return html.replace("</head>", f"{shim}\n</head>", 1)
    return f"{shim}\n{html}"


async def _resolve_chain(registry: ChainRegistry, address: str) -> str:
    """Resolve the chain from the address format, checking the chain if unsure.

    Format alone cannot separate two EVM chains, so a bare ``0x`` address is
    structurally ambiguous. Rather than make the investigator answer a question
    the ledger can answer, ask each candidate whether it has any history for
    this address:

    - exactly one candidate has history -> that is the chain, on evidence;
    - several do -> genuinely ambiguous, and now we can say how much is on each
      instead of just listing names;
    - none do -> nothing to trace anywhere, which is its own answer.

    This is not guessing. A chain with no transactions for an address is not a
    plausible subject, and the check costs one cheap call per candidate.
    """
    matches = registry.detect(address)
    if not matches:
        raise HTTPException(
            status_code=422,
            detail={
                "reason": "unrecognized",
                "message": (
                    "Could not recognize this address format. Supported chains: "
                    f"{', '.join(registry.chains())}."
                ),
                "candidates": [],
            },
        )
    if len(matches) == 1:
        return matches[0]

    seen: dict[str, int] = {}
    unchecked: list[str] = []
    for chain in matches:
        adapter = registry.get(chain)
        try:
            page = await adapter.address_history(
                Address(chain, adapter.canonical_address(address)), limit=_PROBE_LIMIT
            )
        except Exception:  # a provider failure must not silently decide the ledger
            logger.warning("chain probe failed for %s on %s", address, chain, exc_info=True)
            unchecked.append(chain)
            continue
        if page.items:
            seen[chain] = len(page.items)

    # Probing can prove a chain holds NOTHING, which eliminates it. It cannot
    # rank the chains that remain: the adapter merges several capped feeds, so
    # every busy address looks equally full (Binance 14 probes at 75 on polygon
    # against 64 on ethereum). So evidence may narrow the field to one — that is
    # not a guess, it is the only candidate left — but it never picks between
    # two live candidates. Choosing there would be guessing a ledger, and
    # disclosing the guess afterwards does not make the answer less wrong: a
    # confident trace of the wrong chain is a confident claim about the wrong
    # money, and nobody reads the log line.
    active = sorted(seen)
    if len(active) == 1 and not unchecked:
        logger.info("resolved %s to %s by on-chain activity", address, active[0])
        return active[0]
    if active:
        # One live candidate plus a chain we could not read is still not one
        # candidate — the unread chain may hold history too.
        raise HTTPException(
            status_code=422,
            detail={
                "reason": "ambiguous",
                "message": (
                    "This address is active on more than one chain. Which one do you mean?"
                    if not unchecked
                    else "This address is active on "
                    + ", ".join(active)
                    + ", and "
                    + ", ".join(unchecked)
                    + " could not be checked. Please pick one."
                ),
                "candidates": sorted({*active, *unchecked}),
            },
        )
    if unchecked:
        # Nothing was found, but not everything was looked at. Reporting "no
        # transactions" here would state a fact about the blockchain on the
        # strength of an outage — the tool would be confidently wrong about
        # someone's money because a cache was down.
        raise HTTPException(
            status_code=422,
            detail={
                "reason": "ambiguous",
                "message": (
                    "Could not check "
                    + ", ".join(unchecked)
                    + " for this address, so the chain could not be resolved automatically. "
                    "Please pick one."
                ),
                "candidates": list(matches),
            },
        )
    raise HTTPException(
        status_code=422,
        detail={
            "reason": "no_history",
            "message": (
                "No transactions found for this address on any supported chain "
                f"({', '.join(matches)}), so there is nothing to trace."
            ),
            "candidates": list(matches),
        },
    )


async def _safe_run(engine: InvestigationEngine, investigation_id: uuid.UUID) -> None:
    # The engine marks the investigation 'failed' with the error before
    # re-raising; the background task just must not crash the loop.
    try:
        await engine.run(investigation_id)
    except Exception:  # pragma: no cover - status already persisted by the engine
        logger.exception("background investigation %s failed", investigation_id)


def create_app_from_settings(settings: Settings | None = None) -> FastAPI:
    """Production wiring: real providers, durable cache, background runs.

    The engine needs an HTTP client that only exists for the lifespan, so it
    is built there and resolved per request through a holder — routes
    themselves are created once, below.

    ``CIPHERCHAIN_DEMO_API_KEY`` is read from the environment rather than from
    ``Settings`` on purpose: it is not deployment configuration, it is one
    local script handing this process a token it minted a second ago
    (``scripts/demo.sh``). Keeping it out of the settings model keeps it out of
    ``.env.example``, where it would read like something a deployment ought to
    set.
    """
    configure_logging()
    settings = settings or get_settings()
    db_engine: AsyncEngine = create_engine(settings.database_url)
    session_factory = create_session_factory(db_engine)
    holder: dict[str, Any] = {}

    @asynccontextmanager
    async def lifespan(_: FastAPI) -> AsyncIterator[None]:
        try:
            async with httpx.AsyncClient(timeout=25) as http:
                pool = build_provider_pool(
                    settings, http, cache=PostgresProviderCache(session_factory)
                )
                holder["pool"] = pool
                # Labels come from the lifecycle store, not from files: only
                # rows that survived the intel lifecycle may ever name an
                # endpoint (LABEL_INTELLIGENCE.md §4).
                attributor = await build_store_attributor(session_factory)
                holder["engine"] = build_engine(
                    build_chain_registry(pool), session_factory, attributor
                )
                logger.info("CipherChain ready — chains: %s", build_chain_registry(pool).chains())
                yield
        finally:
            holder.clear()
            await db_engine.dispose()

    def engine_provider() -> InvestigationEngine:
        engine = holder.get("engine")
        if engine is None:  # pragma: no cover - only before startup completes
            raise HTTPException(status_code=503, detail="service starting")
        return cast(InvestigationEngine, engine)

    def pool_metrics() -> dict[str, object]:
        pool = holder.get("pool")
        return pool.metrics.snapshot() if pool is not None else {}

    return create_app(
        session_factory,
        engine_provider=engine_provider,
        run_in_background=True,
        pool_metrics=pool_metrics,
        lifespan=lifespan,
        static_dir=STATIC_DIR,
        auth=ApiKeyAuth.from_settings(session_factory, settings),
        demo_api_key=os.environ.get(DEMO_KEY_ENV),
    )
