"""The LTBridge **request boundary** — the outer ASGI layer that owns every
non-API path, exported as a reusable wrapper so an *embedder* gets the same
semantics a standalone LTBridge server has.

Why this is a layer and not middleware
--------------------------------------
Litestar's own ``middleware=[...]`` is wired PER MATCHED ROUTE (each handler's
"ownership layer" stack), never as a true wrapper around the whole app —
confirmed empirically (Task 6/Task 10): a routing-level
``NotFoundException``/``MethodNotAllowedException`` (unmatched path, wrong
method) is raised and turned into a response by Litestar's own
exception-handling layer BEFORE any per-route middleware ever runs, so
per-route middleware never sees it. Worse, Litestar's router silently
NORMALIZES a trailing slash onto an exact route, so ``POST /translate/`` used
to 200-translate (final-review CRITICAL 1) where the Rust oracle's exact actix
matching 405s it.

The fix is a genuine OUTER ASGI layer that runs BEFORE routing and hands the
inner app ONLY the paths it is allowed to see:

* an **exact** match against :data:`API_PATHS` (plus any ``extra_api_paths``
  the embedder owns) -> delegate to the inner app;
* any other path, GET/HEAD -> a real static file off LTBridge's bundled
  frontend (when ``serve_static``), else the oracle-pinned ``Not found`` 404;
* any other path, any other method -> the oracle-pinned generic 405.

Because the boundary never hands a non-exact path to the router, the router's
trailing-slash normalization can never fire.

What is deliberately NOT here
-----------------------------
**Metrics.** A host that mounts LTBridge owns its own request accounting, so
this wrapper observes nothing. The standalone server's Prometheus observation
lives in ``app.create_app``, wrapped AROUND this boundary (see the note there
for why the ordering is outer-metrics / inner-boundary and not the reverse).
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from litestar import Response
from litestar.response.base import ASGIResponse
from litestar.status_codes import (
    HTTP_200_OK,
    HTTP_404_NOT_FOUND,
    HTTP_405_METHOD_NOT_ALLOWED,
)
from litestar.types import (
    ASGIApp,
    LifeSpanReceive,
    LifeSpanScope,
    LifeSpanSend,
    Receive,
    Scope,
    Send,
)
from ltbridge_frontend import get_static_dir

__all__ = ["API_PATHS", "boundary"]

# ---------------------------------------------------------------------------
# Static file resolution (serves the frontend UI off disk)
# ---------------------------------------------------------------------------

_STATIC_DIR: Path = get_static_dir()
_STATIC_ROOT: Path = _STATIC_DIR.resolve()  # cached once — never re-resolved per request

# The routes LTBridge's own router owns. This is an EXACT-match set — a
# trailing slash (e.g. "/translate/") is NOT a member (final-review CRITICAL 1:
# it used to be excluded via `path.rstrip("/")`, which let Litestar's own
# trailing-slash route normalization silently 200-translate `POST
# /translate/`). `boundary()` below checks membership directly and is the ONLY
# place that decides whether a path is "exact" — every other path,
# "/translate/" included, is fully owned by the boundary and never reaches the
# inner app's router at all.
#
# PUBLIC (v0.1.2+): an embedder mounting `create_router` into its own app reads
# this to know which paths LTBridge claims, and extends it with its OWN routes
# via `boundary(..., extra_api_paths=...)` — a host route outside the union
# would otherwise be swallowed by the static/404 arm instead of reaching the
# host's handler. Frozen; never mutate it.
API_PATHS: frozenset[str] = frozenset(
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
# between the routed-405 exception handler in `app.py` and the boundary's own
# 405 arm so the two can never drift apart.
_METHOD_NOT_ALLOWED_BODY = "This resource only supports GET and HEAD."
_METHOD_NOT_ALLOWED_HEADERS = {"allow": "GET, HEAD"}


def _method_not_allowed_asgi_response() -> ASGIResponse:
    """The oracle-pinned generic 405, built once so every call site (the
    boundary's non-GET/HEAD arm and the routed-405 exception handler) shares
    the exact same body/status/headers."""
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


def _resolve_static_path(
    path: str, *, api_paths: frozenset[str] = API_PATHS
) -> tuple[Path, str] | None:
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

    ``api_paths`` defaults to :data:`API_PATHS` and widens to the boundary's
    union when an embedder supplies ``extra_api_paths`` — a host route must
    never be shadowed by a same-named file on the static surface.

    The exclusion below is an EXACT match ONLY — unlike a prior revision, a
    trailing slash (``"/translate/"``) is deliberately NOT excluded here
    (final-review CRITICAL 1). It is instead an ordinary, never-matching
    candidate path, exactly like Rust's own behaviour: actix's route matching
    is exact, so ``"/translate/"`` never reaches the ``/translate`` handler at
    all — it falls through to the embedded static-file service like any other
    miss, and 404s/405s there. Litestar's router, left to its own devices,
    silently NORMALIZES a trailing slash onto the exact route (so ``POST
    /translate/`` used to 200-translate) — the fix is that :func:`boundary`
    never hands a non-exact path to the router at all; see there for the full
    ownership split.
    """
    if path in api_paths:
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


def _static_asgi_response(file_path: Path, *, is_head: bool) -> ASGIResponse:
    """A 200 for a resolved static asset.

    ``ASGIResponse``'s ``is_head_response=True`` path forbids a non-empty body
    outright AND skips setting content-type/content-length entirely — neither
    matches the oracle (HEAD carries the same Content-Length a GET would). So
    HEAD is built as an ordinary 200 with an empty body and an EXPLICIT
    content-length header (added via ``headers=``, which the auto-length
    ``setdefault`` then leaves alone since the key is already present).
    """
    content = file_path.read_bytes()
    return ASGIResponse(
        body=b"" if is_head else content,
        media_type=_guess_mime(file_path.suffix),
        status_code=HTTP_200_OK,
        headers={"content-length": str(len(content))} if is_head else None,
    )


def boundary(
    app: ASGIApp,
    *,
    serve_static: bool = True,
    extra_api_paths: frozenset[str] = frozenset(),
) -> ASGIApp:
    """Wrap ``app`` in LTBridge's request boundary.

    The returned ASGI app applies EXACTLY the non-API-path semantics a
    standalone LTBridge server has, and nothing else — no metrics, no logging,
    no state. Non-HTTP scopes (lifespan, websocket) pass straight through, so
    the wrapper is transparent to the host's own lifespan.

    Args:
        app: The inner ASGI app. Only exact-match API paths ever reach it — so
            for a host app, every route it owns MUST be listed in
            ``extra_api_paths`` or it will never be dispatched.
        serve_static: Serve LTBridge's bundled frontend for GET/HEAD on
            non-API paths (``/`` -> ``index.html``, plus any real asset).
            ``False`` turns the whole static surface off: every non-API
            GET/HEAD becomes the pinned ``Not found`` 404. Use it when the
            host serves its own UI, or wants no UI at all.
        extra_api_paths: The host's OWN exact route paths, unioned with
            :data:`API_PATHS`. Exact matches only — list ``"/proxy/health"``,
            not a prefix; a trailing-slash variant is a different string and
            is deliberately NOT implied.

    Returns:
        An ASGI app. Not a ``Litestar`` instance — mount it as the outermost
        callable your server imports (``granian --interface asgi
        pkg.mod:app``), keeping the inner ``Litestar`` for everything a
        Litestar app is needed for (routes, DI, per-route middleware,
        exception handlers).
    """
    api_paths = API_PATHS | extra_api_paths

    async def boundary_app(
        scope: Scope | LifeSpanScope,
        receive: Receive | LifeSpanReceive,
        send: Send | LifeSpanSend,
    ) -> None:
        if scope["type"] != "http":
            await app(scope, receive, send)  # type: ignore[arg-type]
            return

        path: str = scope.get("path", "/")  # type: ignore[assignment]
        if path in api_paths:
            # The ONLY case that reaches the inner router. Everything else is
            # owned here, so the router's trailing-slash normalization (which
            # used to silently 200-translate `POST /translate/`) can never
            # fire.
            await app(scope, receive, send)  # type: ignore[arg-type]
            return

        method: str = scope.get("method", "UNKNOWN")  # type: ignore[assignment]
        if method not in ("GET", "HEAD"):
            # Rust's embedded static-file service rejects a non-GET/HEAD
            # method in its own guard BEFORE any file lookup happens — so the
            # 405 is identical whether or not the path names a real asset
            # (oracle-verified for `POST /`, `POST /app.js`, and `POST
            # /translate/` alike). Resolving the file first would change
            # nothing but cost a stat.
            await _method_not_allowed_asgi_response()(scope, receive, send)  # type: ignore[arg-type]
            return

        resolved = _resolve_static_path(path, api_paths=api_paths) if serve_static else None
        if resolved is not None:
            file_path, _canonical = resolved
            response = _static_asgi_response(file_path, is_head=method == "HEAD")
        else:
            # A path Rust's actix router never matches either — it falls
            # through to the embedded static-file service, which allows
            # GET/HEAD through its method guard and then misses on the file
            # lookup: the pinned "Not found" 404.
            response = _not_found_asgi_response(is_head=method == "HEAD")
        await response(scope, receive, send)  # type: ignore[arg-type]

    return boundary_app
