"""Litestar application implementing the LibreTranslate-compatible API.

Port of LTEngine's main.rs (--features api), translated to Python/Litestar.
"""

from __future__ import annotations

import logging
import time
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

import httpx
from litestar import Litestar, Request, Response, get
from litestar.datastructures import State
from litestar.exceptions import MethodNotAllowedException, NotFoundException
from litestar.response.base import ASGIResponse
from litestar.status_codes import (
    HTTP_200_OK,
    HTTP_404_NOT_FOUND,
    HTTP_405_METHOD_NOT_ALLOWED,
)
from litestar.types import LifeSpanReceive, LifeSpanScope, LifeSpanSend, Receive, Scope, Send
from ltengine_frontend import get_static_dir
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
from .routes import create_router

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Static file resolution (serves the frontend UI off disk)
# ---------------------------------------------------------------------------

_STATIC_DIR: Path = get_static_dir()
_STATIC_ROOT: Path = _STATIC_DIR.resolve()  # cached once — never re-resolved per request

# The routes Litestar itself owns. This is an EXACT-match set — a trailing
# slash (e.g. "/translate/") is NOT a member (final-review CRITICAL 1: it
# used to be excluded via `path.rstrip("/")`, which let Litestar's own
# trailing-slash route normalization silently 200-translate `POST
# /translate/`). `_LTEngineApp.__call__` below checks membership directly and
# is the ONLY place that decides whether a path is "exact" — every other
# path, "/translate/" included, is fully owned by the outer layer and never
# reaches Litestar's router at all.
_API_PATHS = frozenset(
    {
        "/health",
        "/languages",
        "/frontend/settings",
        "/detect",
        "/translate",
        "/translate_file",
        "/suggest",
        "/metrics",
    }
)

# Task 6's oracle-pinned generic 405 (actix-web-static-files' method guard:
# GET/HEAD only, identical body/headers regardless of which route or path was
# actually hit — oracle-verified again here for the static surface itself:
# POST / and POST on a real asset both 405 with this exact body). Shared
# between the routed-405 exception handler below and the static-surface 405
# path in `create_app` (Task 11 fix wave 2) so the two can never drift apart.
_METHOD_NOT_ALLOWED_BODY = "This resource only supports GET and HEAD."
_METHOD_NOT_ALLOWED_HEADERS = {"allow": "GET, HEAD"}


def _method_not_allowed_asgi_response() -> ASGIResponse:
    """The oracle-pinned generic 405, built once so every call site (the
    static-hit branch, the "owned" non-exact-path branch, and the routed-405
    exception handler) shares the exact same body/status/headers."""
    return ASGIResponse(
        body=_METHOD_NOT_ALLOWED_BODY,
        media_type="text/plain",
        status_code=HTTP_405_METHOD_NOT_ALLOWED,
        headers=_METHOD_NOT_ALLOWED_HEADERS,
    )


# Final-review CRITICAL 2 / Minor 7 (ledger T6-5): the OTHER oracle-pinned
# generic body — actix's bare `HttpResponse::NotFound().body("Not found")`,
# hit both for a genuinely unmatched path AND for a GET/HEAD request against
# an API path whose actix route guard doesn't match (falls through to the
# same embedded static-file miss). Oracle-verified: NO content-type header at
# all (not an empty one), and on HEAD an EMPTY body with the byte count a GET
# would have carried in Content-Length. `ASGIResponse.__init__` always
# `setdefault`s a content-type whenever the status allows a body, with no
# supported way to opt out through the public constructor — so the header is
# stripped by hand, once, here; every code path that produces this response
# goes through this single builder so body/status/header-omission can never
# drift between them.
_NOT_FOUND_BODY = b"Not found"


def _not_found_asgi_response(*, is_head: bool = False) -> ASGIResponse:
    resp = ASGIResponse(
        body=b"" if is_head else _NOT_FOUND_BODY,
        status_code=HTTP_404_NOT_FOUND,
        headers={"content-length": str(len(_NOT_FOUND_BODY))} if is_head else None,
    )
    del resp.headers["content-type"]
    return resp


class _NotFoundResponse(Response[bytes]):
    """A ``Response`` whose ASGI conversion is
    :func:`_not_found_asgi_response` byte-for-byte — the only way to omit the
    content-type header entirely, since ``Response.to_asgi_response()``'s own
    pipeline always sets one via ``ASGIResponse.__init__``'s ``setdefault``.
    Subclasses ``Response`` (rather than duck-typing an unrelated object)
    purely so the ``exception_handlers`` mapping's
    ``Callable[[Request, ExceptionT], Response]`` contract still type-checks.
    """

    __slots__ = ("_is_head",)

    def __init__(self, *, is_head: bool) -> None:
        super().__init__(content=_NOT_FOUND_BODY, status_code=HTTP_404_NOT_FOUND)
        self._is_head = is_head

    def to_asgi_response(self, *args: Any, **kwargs: Any) -> ASGIResponse:  # type: ignore[override]
        return _not_found_asgi_response(is_head=self._is_head)


def _guess_mime(suffix: str) -> str:
    return {
        ".html": "text/html",
        ".js": "application/javascript",
        ".css": "text/css",
        ".json": "application/json",
        ".png": "image/png",
        ".ico": "image/x-icon",
        ".svg": "image/svg+xml",
    }.get(suffix.lower(), "application/octet-stream")


def _resolve_static_path(path: str) -> tuple[Path, str] | None:
    """Resolve a request PATH — independent of method — to a real static
    asset. Returns ``(file_path, canonical_label)`` on a hit, else ``None``.

    ``canonical_label`` is the path actually SERVED — derived from the
    resolved file's location relative to ``_STATIC_ROOT`` — never the raw
    request path. Aliasing forms of the same request (extra slashes, a "."
    segment, an encoded ".." that survives client-side URL normalization)
    must collapse to exactly ONE Prometheus label, or an attacker can mint
    unbounded label cardinality for free by varying the raw path. Root ``/``
    is labeled ``"/"`` specifically — kept distinct from an explicit
    ``/index.html`` request even though both currently serve the same file;
    a deliberate, documented choice, not an oversight.

    ``index.html`` is served ONLY at ``/``. Every other path is served iff a
    real file exists under ``_STATIC_DIR`` — resolved and bounds-checked
    against path traversal. Method-agnostic on purpose: the caller decides
    what a GET, HEAD, or any other method does with a hit (parity: Rust's
    embedded static-file service 200s GET/HEAD and 405s everything else on
    the exact same path set, so both outcomes need the same resolution).

    The ``_API_PATHS`` exclusion below is an EXACT match ONLY — unlike a
    prior revision, a trailing slash (``"/translate/"``) is deliberately NOT
    excluded here (final-review CRITICAL 1). It is instead an ordinary,
    never-matching candidate path, exactly like Rust's own behaviour: actix's
    route matching is exact, so ``"/translate/"`` never reaches the
    ``/translate`` handler at all — it falls through to the embedded
    static-file service like any other miss, and 404s/405s there. Litestar's
    router, left to its own devices, silently NORMALIZES a trailing slash
    onto the exact route (so ``POST /translate/`` used to 200-translate) —
    the fix is that the caller (``_LTEngineApp.__call__``) now never hands a
    non-exact path to Litestar's router at all; see there for the full
    ownership split.
    """
    if path in _API_PATHS:
        return None

    if path == "/":
        file_path = _STATIC_DIR / "index.html"
        canonical = "/"
    else:
        candidate = (_STATIC_DIR / path.lstrip("/")).resolve()
        if not candidate.is_relative_to(_STATIC_ROOT):
            return None
        file_path = candidate
        canonical = "/" + file_path.relative_to(_STATIC_ROOT).as_posix()

    if not file_path.is_file():
        return None
    return file_path, canonical


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
    # after the final-review CRITICAL 1 fix (`_LTEngineApp.__call__` now owns
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
    llm_client = LLMClient(
        url=config.llm_url,
        model=config.llm_model,
        api_key=config.llm_api_key,
        http_client=http_client,
    )

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
        # dispatch it feeds — see the `logging_config=None` note on `_LTEngineApp`
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
    # Outer ASGI layer: static-file serving + request metrics
    # ------------------------------------------------------------------
    #
    # Litestar's own `middleware=[...]` is wired PER MATCHED ROUTE (each
    # handler's "ownership layer" stack), never as a true wrapper around the
    # whole app — confirmed empirically (Task 6/Task 10): a routing-level
    # `NotFoundException`/`MethodNotAllowedException` (unmatched path, wrong
    # method) is raised and turned into a response by Litestar's own
    # exception-handling layer BEFORE any per-route middleware ever runs, so
    # per-route middleware never sees it. That made the old
    # `StaticFilesMiddleware` dead code for exactly the paths it existed to
    # handle (root `/` and unknown paths — both always unmatched), and made
    # `ltengine_requests_total` blind to every routing-level 404/405.
    #
    # The fix is a genuine outer layer: override `Litestar.__call__` itself
    # (the actual outermost ASGI entry point) on a thin subclass, rather than
    # adding a second parallel mechanism alongside it. This runs BEFORE
    # routing for every HTTP request, so it can (a) serve a real static file
    # straight off disk without ever entering Litestar's router — GET gets
    # the full body; HEAD gets an empty body with the SAME Content-Length a
    # GET would carry (oracle-verified: Rust's HEAD `/` and HEAD on a real
    # asset both report the full content-length and content-type, no body);
    # any other method gets the Task 6 oracle-pinned generic 405
    # (oracle-verified: identical body/headers to the routed 405 case) — and
    # (b) always record exactly one metrics observation per request, using
    # `_resolve_static_path`'s canonical (bounded) label for a static hit or
    # static-surface 405, the route's `path_template` when Litestar's own
    # routing succeeded, or a fixed `"__unmatched__"` sentinel when none of
    # those resolved (never the raw request path, which is attacker-
    # controlled and would blow up label cardinality on its own).
    class _LTEngineApp(Litestar):
        async def __call__(
            self,
            scope: Scope | LifeSpanScope,
            receive: Receive | LifeSpanReceive,
            send: Send | LifeSpanSend,
        ) -> None:
            if scope["type"] != "http":
                await super().__call__(scope, receive, send)
                return

            method: str = scope.get("method", "UNKNOWN")  # type: ignore[assignment]
            path: str = scope.get("path", "/")  # type: ignore[assignment]
            # Final-review CRITICAL 1: an EXACT match against one of the 8
            # real API paths is the ONLY case that still delegates to
            # Litestar's own router — every other path (a trailing slash on
            # an API path, any other unmatched path) is now fully OWNED here
            # and never reaches Litestar's routing at all, so its
            # trailing-slash normalization (which used to silently
            # 200-translate `POST /translate/`) can never fire.
            # `_resolve_static_path` only needs to run for the non-exact
            # case — an exact API path is never a candidate static asset.
            is_exact_api_path = path in _API_PATHS
            resolved = None if is_exact_api_path else _resolve_static_path(path)

            start_time = time.perf_counter()
            status_code = 500  # default to 500 if an exception occurs
            static_path_label = resolved[1] if resolved is not None else None

            async def send_with_metrics(message: dict[str, Any]) -> None:
                nonlocal status_code
                if message["type"] == "http.response.start":
                    status_code = message["status"]
                await send(message)  # type: ignore[arg-type]

            try:
                if resolved is not None:
                    file_path, _canonical = resolved
                    if method in ("GET", "HEAD"):
                        content = file_path.read_bytes()
                        mime = _guess_mime(file_path.suffix)
                        is_head = method == "HEAD"
                        # ASGIResponse's `is_head_response=True` path forbids
                        # a non-empty body outright AND skips setting
                        # content-type/content-length entirely — neither
                        # matches the oracle (HEAD carries the same
                        # Content-Length a GET would). So HEAD is built as an
                        # ordinary 200 with an empty body and an EXPLICIT
                        # content-length header (added via `headers=`, which
                        # the auto-length `setdefault` below then leaves
                        # alone since the key is already present).
                        asgi_response = ASGIResponse(
                            body=b"" if is_head else content,
                            media_type=mime,
                            status_code=HTTP_200_OK,
                            headers={"content-length": str(len(content))} if is_head else None,
                        )
                    else:
                        # Same static-file-service fallback as Litestar's own
                        # routed 405 (Task 6) — oracle-verified identical
                        # body/headers for a non-GET/HEAD hit on root or a
                        # real asset. Reuses the same builder; never a second
                        # hand-built variant.
                        asgi_response = _method_not_allowed_asgi_response()
                    await asgi_response(scope, receive, send_with_metrics)  # type: ignore[arg-type]
                elif is_exact_api_path:
                    await super().__call__(scope, receive, send_with_metrics)  # type: ignore[arg-type]
                else:
                    # Final-review CRITICAL 1/2: everything reaching here is
                    # a path Rust's actix router never matches either — it
                    # falls through to the embedded static-file service,
                    # whose method guard (GET/HEAD only) decides the shape:
                    # GET/HEAD -> the pinned "Not found" 404 (oracle-verified
                    # for both a plain unmatched path AND a wrong-method
                    # GET/HEAD against a real API path, e.g. `GET
                    # /translate`, a POST-only route — that case instead
                    # reaches `_method_not_allowed_handler` below, which
                    # builds the identical response); anything else -> the
                    # pinned generic 405. Built through the exact same
                    # builders the static-hit branch above uses, so the two
                    # can never drift.
                    if method in ("GET", "HEAD"):
                        asgi_response = _not_found_asgi_response(is_head=method == "HEAD")
                    else:
                        asgi_response = _method_not_allowed_asgi_response()
                    await asgi_response(scope, receive, send_with_metrics)  # type: ignore[arg-type]
            finally:
                duration = time.perf_counter() - start_time
                path_label = static_path_label or scope.get("path_template") or "__unmatched__"
                requests_total.labels(endpoint=path_label, method=method, status=status_code).inc()
                request_duration_seconds.labels(
                    endpoint=path_label, method=method, status=status_code
                ).observe(duration)

    return _LTEngineApp(
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
        exception_handlers={
            LlmError: _llm_error_handler,
            NotFoundException: _not_found_handler,
            MethodNotAllowedException: _method_not_allowed_handler,
        },
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
    `LTENGINE_*` environment variables from the parsed CLI args and points
    Granian's `target` at `"ltengine.server.app:app_factory"` with `factory=True`.
    """
    from ..config import Config

    return create_app(Config.from_env())
