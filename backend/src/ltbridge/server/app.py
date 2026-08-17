"""Litestar application implementing the LibreTranslate-compatible API.

Port of LTEngine's main.rs (--features api), translated to Python/Litestar.
"""

from __future__ import annotations

import logging
import time
from contextlib import asynccontextmanager
from typing import Any

import httpx
from litestar import Litestar, Request, Response, get
from litestar.datastructures import State
from litestar.exceptions import MethodNotAllowedException, NotFoundException
from litestar.status_codes import HTTP_200_OK, HTTP_405_METHOD_NOT_ALLOWED
from litestar.types import (
    ASGIApp,
    ExceptionHandlersMap,
    LifeSpanReceive,
    LifeSpanScope,
    LifeSpanSend,
    Receive,
    Scope,
    Send,
)
from prometheus_client import (
    CONTENT_TYPE_LATEST,
    CollectorRegistry,
    Counter,
    Histogram,
    generate_latest,
)

from ..config import Config
from ..core.errors import LlmError
from ..llm_client import LLMClient
from .boundary import (
    _METHOD_NOT_ALLOWED_BODY,
    _METHOD_NOT_ALLOWED_HEADERS,
    _NotFoundResponse,
    _resolve_static_path,
    boundary,
)
from .boundary import API_PATHS as _API_PATHS
from .routes import create_router

logger = logging.getLogger(__name__)

# `_API_PATHS`, `_resolve_static_path`, `_NotFoundResponse` and the two
# `_METHOD_NOT_ALLOWED_*` constants moved to `.boundary` in v0.1.2 (the
# boundary became an exportable wrapper for embedders). They are re-imported
# here — rather than referenced through the module — because this module's own
# exception handlers and metrics layer use them, and because both names have
# been importable from `ltbridge.server.app` since Task 11.

# ---------------------------------------------------------------------------
# App-level exception handlers — every non-2xx body Litestar's own machinery
# would otherwise shape as ``{"status_code": ..., "detail": ...}`` is
# re-shaped here to match the observed Rust (actix) output byte-for-byte.
# See .superpowers/sdd/task-6-report.md for the transcribed reference bodies.
# ---------------------------------------------------------------------------


def _llm_error_handler(request: Request[Any, Any, State], exc: LlmError) -> Response:
    return Response(
        content={"error": exc.message},
        status_code=exc.http_status,
        media_type="application/json",
    )


def _not_found_handler(request: Request[Any, Any, State], exc: NotFoundException) -> Response:
    # Rust's actual behaviour: no explicit catch-all route exists, so an
    # unmatched path falls through to the embedded static-file service, whose
    # miss path is actix's bare `HttpResponse::NotFound().body("Not found")` —
    # plain text, NO Content-Type header at all. Effectively unreachable
    # after the final-review CRITICAL 1 fix (`_LTBridgeApp.__call__` now owns
    # every non-exact-API path itself and never hands one to Litestar's
    # router, so Litestar's own routing never raises `NotFoundException` in
    # practice) — kept registered defensively, and built through
    # `_NotFoundResponse` (the same builder every REACHABLE 404 uses), so it
    # can't drift even as dead code.
    return _NotFoundResponse(is_head=request.method == "HEAD")


def _method_not_allowed_handler(
    request: Request[Any, Any, State], exc: MethodNotAllowedException
) -> Response:
    # Litestar hands off here when the PATH matched a real route but the
    # METHOD didn't. Rust has no such distinct case: a matched-path/wrong-
    # method request falls through to the SAME embedded static-file service
    # as any other miss, and that service's own method guard (GET/HEAD only)
    # decides the shape — final-review CRITICAL 2, oracle-verified for both a
    # POST-only route (`GET /translate`) and a GET-only route (`HEAD
    # /languages`): GET/HEAD is ALLOWED by the guard, then 404s on the file
    # lookup (there's no file named "translate"/"languages"). Any OTHER
    # method (POST/DELETE/PUT/...) is rejected by the guard itself, before a
    # file lookup ever happens — the generic 405, always "GET and HEAD",
    # regardless of which route/method was actually intended.
    if request.method in ("GET", "HEAD"):
        return _NotFoundResponse(is_head=request.method == "HEAD")
    return Response(
        content=_METHOD_NOT_ALLOWED_BODY,
        status_code=HTTP_405_METHOD_NOT_ALLOWED,
        media_type="text/plain",
        headers=_METHOD_NOT_ALLOWED_HEADERS,
    )


# ---------------------------------------------------------------------------
# Public embedding surface — an embedder (e.g. the smartech-translate proxy)
# mounts ``create_router(...)`` (re-exported from ``server/routes.py``) into
# ITS OWN Litestar app. These two functions are the extracted pieces of
# ``create_app`` an embedder needs: the same exception-handler callables (so
# error bodies stay byte-identical to a standalone LTBridge server) and the
# ``LLMClient`` construction logic (so the embedder can share its own pooled
# ``httpx.AsyncClient`` instead of LTBridge opening a second one). ``create_app``
# below calls both — single source of truth, proven unchanged by the
# differential suite.
# ---------------------------------------------------------------------------


def exception_handlers() -> ExceptionHandlersMap:
    """The exception-handler mapping ``create_app`` registers on its ``Litestar``.

    Returns the SAME callables (not copies/wrappers) so an embedder's app
    produces byte-identical error bodies to a standalone LTBridge server.
    """
    return {
        LlmError: _llm_error_handler,
        NotFoundException: _not_found_handler,
        MethodNotAllowedException: _method_not_allowed_handler,
    }


def build_llm_client(config: Config, http_client: httpx.AsyncClient) -> LLMClient:
    """Build the ``LLMClient`` ``create_app`` uses, against a caller-supplied
    ``httpx.AsyncClient`` — extracted so an embedder can share its own pooled
    client rather than LTBridge opening a second one.
    """
    return LLMClient(
        url=config.llm_url,
        model=config.llm_model,
        api_key=config.llm_api_key,
        http_client=http_client,
    )


# ---------------------------------------------------------------------------
# Application factory
# ---------------------------------------------------------------------------


def create_app(config: Config) -> Litestar:
    """Build and return the Litestar application for the given config."""

    http_client = httpx.AsyncClient(
        timeout=None if config.llm_timeout == 0 else httpx.Timeout(config.llm_timeout),
        # reqwest follows redirects by default (up to 10); httpx's default is
        # follow_redirects=False. Matching reqwest's ceiling for parity — the
        # differential mock can't emit a redirect, so this is reasoned from
        # reqwest's documented defaults rather than oracle-captured; covered
        # by a unit test on the constructed client instead (see task-6-report,
        # Fix wave 2).
        follow_redirects=True,
        max_redirects=10,
    )
    llm_client = build_llm_client(config, http_client)

    @asynccontextmanager
    async def lifespan(app: Litestar):
        logging.basicConfig(
            level=logging.DEBUG if config.verbose else logging.INFO,
            format="%(asctime)s %(name)s %(levelname)s %(message)s",
        )
        # httpx logs "HTTP Request: POST ..." at INFO for every upstream call — a
        # third-party access-log nobody asked for, live on the request path since
        # its logger propagates to root. Under concurrent load this is a real,
        # measured cost (B1 profiling in bench/REPORT.md: cProfile traced roughly a
        # third of hot-path time to LogRecord construction + the queue-handler
        # dispatch it feeds — see the `logging_config=None` note on `_LTBridgeApp`
        # below for the other half of this fix). httpx's `_client.py` calls
        # `logger.info(...)` at exactly two call sites and nowhere else — it has no
        # WARNING/ERROR logging of its own; a transport failure (connect refused,
        # timeout, ...) surfaces as a raised exception, not a log record, and is
        # handled by `LLMClient`/`errors.map_llm_failure` regardless of this
        # logger's level. So WARNING here silences httpx's per-request noise
        # entirely — it does not "keep failures visible on a quieter level", there
        # is no such record to keep.
        logging.getLogger("httpx").setLevel(logging.WARNING)

        # Exposed on app.state so the constructed client is inspectable from
        # tests (and, incidentally, any future admin/health introspection)
        # without reaching into this closure's locals.
        app.state.http_client = http_client

        if not config.llm_model:
            logger.info("Resolving model from %s/v1/models …", config.llm_url)
            resolved = await llm_client.resolve_model()
            logger.info("Using model: %s", resolved)
        else:
            logger.info("Using model: %s", config.llm_model)

        yield

        await http_client.aclose()

    # ------------------------------------------------------------------
    # Prometheus metrics
    # ------------------------------------------------------------------

    registry = CollectorRegistry()
    # Final-review IMPORTANT 5: metric NAMES and LABEL NAMES now match the
    # oracle's actix_web_prom schema exactly (oracle-verified via /metrics on
    # the Rust reference: `ltengine_http_requests_total{endpoint,method,
    # status}` / `ltengine_http_requests_duration_seconds{endpoint,method,
    # status}`), so an actix_web_prom-shaped dashboard is a drop-in over
    # either binary. `endpoint` replaces the old `path` label name only — the
    # VALUE it carries is unchanged (still the canonical/bounded label from
    # `_resolve_static_path` or `path_template`, never the raw request path;
    # see `__call__` below). The histogram buckets stay our own (amended,
    # LLM-latency-appropriate) set, not actix_web_prom's tiny sub-10s
    # defaults — only the name/labels are adopted, not the bucket boundaries.
    #
    # NOTE (LTBridge rename): these two metric names keep the `ltengine_`
    # prefix deliberately — it is the actix_web_prom COMPATIBILITY schema
    # (drop-in dashboards vs. the Rust LTEngine server), not a branding
    # choice, so it does NOT follow the ltengine -> ltbridge project rename.
    requests_total = Counter(
        "ltengine_http_requests_total",
        "Total number of HTTP requests",
        ["endpoint", "method", "status"],
        registry=registry,
    )
    request_duration_seconds = Histogram(
        "ltengine_http_requests_duration_seconds",
        "HTTP request duration in seconds for all requests",
        ["endpoint", "method", "status"],
        buckets=(0.05, 0.1, 0.25, 0.5, 1, 2.5, 5, 10, 30, 60, 120, 300, float("inf")),
        registry=registry,
    )

    # ------------------------------------------------------------------
    # Route handlers
    # ------------------------------------------------------------------

    router = create_router(config, llm_client)

    @get("/metrics", name="metrics")
    async def metrics() -> Response:
        # Minor 6: CONTENT_TYPE_LATEST ("text/plain; version=0.0.4;
        # charset=utf-8") already carries its own charset param. Passing it
        # as `media_type=` let ASGIResponse's `setdefault` treat it as a bare
        # "text/plain"-family type and APPEND a second "; charset=utf-8" —
        # doubled. Passing it via `headers=` instead pre-empts that
        # setdefault (the key is already present), so the header lands
        # exactly as CONTENT_TYPE_LATEST, once — matches the oracle's
        # `text/plain; version=0.0.4; charset=utf-8` exactly. `content` is
        # already `bytes` (`generate_latest`), so `Response.render` passes it
        # through unconditionally regardless of `media_type` — no
        # serialization ever depended on `media_type` being set here.
        return Response(
            content=generate_latest(registry),
            status_code=HTTP_200_OK,
            headers={"content-type": CONTENT_TYPE_LATEST},
        )

    # ------------------------------------------------------------------
    # Outer ASGI layer: request metrics, wrapped around the request boundary
    # ------------------------------------------------------------------
    #
    # The boundary itself (static-file serving, the pinned 404/405, the
    # exact-`_API_PATHS` delegation gate) now lives in `.boundary` and is
    # exported for embedders — see that module for why it has to be a true
    # outer ASGI layer rather than Litestar `middleware=[...]`.
    #
    # What stays here is METRICS, and it wraps the boundary from OUTSIDE.
    # `boundary()` deliberately observes nothing (a host owns its own request
    # accounting), so the two compose as:
    #
    #     _LTBridgeApp.__call__   ->  observe  ->  boundary(...)  ->  router
    #
    # NOTE the ordering: metrics OUTSIDE, boundary INSIDE — not the reverse.
    # The boundary answers every non-exact path ITSELF and never calls the
    # inner app for one, so metrics placed inside would go blind to exactly
    # the requests Task 10/11 added them for (routing-level 404s, the pinned
    # 405, and every static hit). `test_unknown_path_404_is_counted_with_
    # bounded_sentinel_label` and the static-label alias tests are the guard.
    #
    # The cost of that ordering is that both layers resolve the static path
    # for a non-API request: the boundary to decide the response, this layer
    # to get the canonical (bounded) Prometheus label. That is one extra
    # `stat` on the static surface only — an exact API path (the hot
    # `/translate` route) short-circuits before either call. The alternative,
    # threading a label back out of the boundary, would put metrics into the
    # embedder-facing contract for no measurable gain.
    #
    # Label choice (unchanged): `_resolve_static_path`'s canonical label for a
    # static hit or static-surface 405, the route's `path_template` when
    # Litestar's own routing succeeded, or a fixed `"__unmatched__"` sentinel
    # when neither resolved — never the raw request path, which is
    # attacker-controlled and would blow up label cardinality on its own.
    class _LTBridgeApp(Litestar):
        _boundary_app: ASGIApp

        def __init__(self, *args: Any, **kwargs: Any) -> None:
            super().__init__(*args, **kwargs)
            # `self._routed` is this app's OWN routing entry point, so the
            # standalone server is literally `boundary(<the router>)` with the
            # default arguments — the same call an embedder makes.
            self._boundary_app = boundary(self._routed)

        async def _routed(
            self,
            scope: Scope | LifeSpanScope,
            receive: Receive | LifeSpanReceive,
            send: Send | LifeSpanSend,
        ) -> None:
            """Litestar's own ASGI entry point, unwrapped."""
            await super().__call__(scope, receive, send)

        async def __call__(
            self,
            scope: Scope | LifeSpanScope,
            receive: Receive | LifeSpanReceive,
            send: Send | LifeSpanSend,
        ) -> None:
            if scope["type"] != "http":
                await self._boundary_app(scope, receive, send)  # type: ignore[arg-type]
                return

            method: str = scope.get("method", "UNKNOWN")  # type: ignore[assignment]
            path: str = scope.get("path", "/")  # type: ignore[assignment]
            # An exact API path is never a candidate static asset, so the
            # resolver only runs for the boundary-owned case.
            resolved = None if path in _API_PATHS else _resolve_static_path(path)
            static_path_label = resolved[1] if resolved is not None else None

            start_time = time.perf_counter()
            status_code = 500  # default to 500 if an exception occurs

            async def send_with_metrics(message: dict[str, Any]) -> None:
                nonlocal status_code
                if message["type"] == "http.response.start":
                    status_code = message["status"]
                await send(message)  # type: ignore[arg-type]

            try:
                await self._boundary_app(scope, receive, send_with_metrics)  # type: ignore[arg-type]
            finally:
                duration = time.perf_counter() - start_time
                path_label = static_path_label or scope.get("path_template") or "__unmatched__"
                requests_total.labels(endpoint=path_label, method=method, status=status_code).inc()
                request_duration_seconds.labels(
                    endpoint=path_label, method=method, status=status_code
                ).observe(duration)

    return _LTBridgeApp(
        route_handlers=[
            router,
            metrics,
        ],
        lifespan=[lifespan],
        debug=config.verbose,
        # Left at Litestar's default (`Empty`), the framework silently installs its own
        # `LoggingConfig()` — a root-logger `QueueHandler` backed by a background-thread
        # `QueueListener` (Python 3.12's native dictConfig queue support; see
        # litestar/logging/config.py `default_handlers`/`configure_root_logger=True`).
        # That pipeline made the `logging.basicConfig(...)` call in `lifespan()` above a
        # silent no-op (root already had a handler, and `basicConfig` refuses to touch an
        # already-configured root logger) and routed every log record — including every
        # httpx per-request line — through a cross-thread queue. B1 profiling attributed a
        # large share of the C=32 wrapper-overhead gap to exactly that: LogRecord
        # construction, queue put/get lock contention, and the listener thread's dequeue
        # loop, all paid per request under load. `None` disables it outright, so our own
        # `basicConfig` call is the one and only (synchronous, single-thread) logging
        # config actually in effect. See bench/REPORT.md, "B1 optimizations".
        #
        # Caveat: Litestar's own exception middleware only logs an UNHANDLED exception's
        # traceback when `app.logging_config` is truthy (`middleware.py`:
        # `if litestar_app.logging_config and (logger := litestar_app.logger): ...
        # handle_exception_logging(...)`) — with `logging_config=None` that path is dead,
        # so a genuine bug that isn't one of the three types below (LlmError,
        # NotFoundException, MethodNotAllowedException) now logs NOTHING through
        # Litestar's own machinery, `--verbose`/`debug=True` included. `debug=True` still
        # puts the traceback in the HTTP response body for local dev, but nothing lands
        # in the server's own logs/console. If that gap is ever felt in practice, the fix
        # is an explicit `except Exception` handler here that logs via `logger` before
        # re-raising/responding — not reverting this flag (that brings the queue pipeline
        # back).
        logging_config=None,
        exception_handlers=exception_handlers(),
    )


def app_factory() -> Litestar:
    """Zero-arg entry point Granian imports by string in each worker process.

    Granian workers are spawned via `multiprocessing`; on Windows and macOS the
    start method is 'spawn' (no fork available), so the worker re-imports this
    module and calls this factory instead of receiving a pickled app object — a
    live `Litestar` app built by `create_app()` holds non-picklable closures
    (e.g. the `lifespan` context manager), so passing the instance itself across
    the process boundary fails there (it only worked on 'fork' platforms, where
    the child inherits memory instead of unpickling). `__main__.py` sets the
    `LTBRIDGE_*` environment variables from the parsed CLI args and points
    Granian's `target` at `"ltbridge.server.app:app_factory"` with `factory=True`.
    """
    from ..config import Config

    return create_app(Config.from_env())
