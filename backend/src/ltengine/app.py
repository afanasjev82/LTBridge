"""Litestar application implementing the LibreTranslate-compatible API.

Port of LTEngine's main.rs (--features api), translated to Python/Litestar.
"""

from __future__ import annotations

import json
import logging
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

import httpx
from litestar import Litestar, Request, Response, get, post
from litestar.datastructures import State
from litestar.exceptions import MethodNotAllowedException, NotFoundException
from litestar.status_codes import (
    HTTP_200_OK,
    HTTP_400_BAD_REQUEST,
    HTTP_403_FORBIDDEN,
    HTTP_404_NOT_FOUND,
    HTTP_405_METHOD_NOT_ALLOWED,
    HTTP_501_NOT_IMPLEMENTED,
)
from litestar.types import ASGIApp, Receive, Scope, Send
from ltengine_frontend import get_static_dir
from prometheus_client import (
    CONTENT_TYPE_LATEST,
    CollectorRegistry,
    Counter,
    Histogram,
    generate_latest,
)

from . import errors
from .config import Config
from .errors import LlmError
from .languages import LANGUAGES, detect_language, get_language_by_code
from .llm_client import LLMClient
from .prompt import PromptBuilder
from .streaming import respond_with_grace
from .token_budget import output_cap

logger = logging.getLogger(__name__)

_PUNCTUATION = set("!?.,;。")

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _improve_formatting(source: str, translation: str) -> str:
    """Post-process translation output to align punctuation and case with source.

    Direct port of improve_formatting() in LTEngine's main.rs.
    """
    t = translation.strip()

    if not source:
        return ""
    if not t:
        return source

    q_last = source[-1]
    t_last = t[-1]

    if q_last in _PUNCTUATION:
        if q_last != t_last:
            if t_last in _PUNCTUATION:
                t = t[:-1]
            t += q_last
    elif t_last in _PUNCTUATION:
        t = t[:-1]

    if source.islower():
        t = t.lower()
    elif source.isupper():
        t = t.upper()
    elif source and t:
        if source[0].islower() and t[0].isupper():
            t = t[0].lower() + t[1:]
        elif source[0].isupper() and t[0].islower():
            t = t[0].upper() + t[1:]

    return t.strip()


def _validate_format(fmt: str) -> str | None:
    """Return an error message if fmt is not supported, else None."""
    if fmt not in ("text", "html"):
        return "Invalid format. Supported formats: text, html"
    return None


def _json_400(message: str) -> Response:
    """Build a Rust-matching ``{"error": ...}`` 400 response."""
    return Response(
        content={"error": message},
        status_code=HTTP_400_BAD_REQUEST,
        media_type="application/json",
    )


async def _parse_translate_payload(request: Request[Any, Any, State]) -> dict[str, Any] | Response:
    """Parse + type-validate a ``/translate`` or ``/detect`` request body.

    Port of main.rs's shared ``parse_payload`` (used by BOTH handlers — the
    Rust ``detect`` handler calls the exact same function as ``translate``)
    plus the typed-field half of serde's derived ``TranslateRequest``
    deserialize:

    - A content-type outside JSON / ``application/x-www-form-urlencoded`` /
      ``multipart/form-data`` is rejected with ``"Unsupported content-type"``
      before any parsing is attempted (Rust: the trailing ``else`` branch of
      ``parse_payload``'s content-type match).
    - For JSON, a syntax failure 400s via :func:`errors.json_deserialize_error`
      (Task 6); a present-but-wrong-typed field (e.g. ``"q":123`` where
      ``q: Option<String>``) 400s via :func:`errors.serde_invalid_type_error` —
      both BEFORE any handler-level (``check_params``) validation runs,
      matching Rust's parse-then-validate ordering exactly (``check_params``
      never runs on a payload that failed to parse/type-check).
    - For form/multipart, Rust ALSO type-checks at parse time — this is a
      correction of a prior version of this docstring, which incorrectly
      claimed "HTTP form encoding has no wire-level type system, so no
      typed-field check applies there." In fact ``parse_payload`` deserializes
      form bodies into the SAME typed ``TranslateRequest`` struct via actix's
      ``web::Form<TranslateRequest>`` (serde_urlencoded) and multipart bodies
      via ``MultipartForm<MPTranslateRequest>`` (each field ``Option<MPText<T>>``).
      The only non-string field is ``alternatives: Option<u32>``
      (``Option<MPText<u32>>`` for multipart); a malformed value there fails
      at the actix EXTRACTOR — before ``check_params`` — with a shape that
      differs from both the JSON path and from the OTHER form content-type,
      oracle-verified (``alternatives=lots``): see :func:`_check_form_alternatives`.

    Returns the parsed body dict, or a `Response` the caller must return
    unchanged (check via ``isinstance(result, Response)``).
    """
    content_type = request.content_type[0] if request.content_type else ""

    if "application/json" in content_type:
        raw = await request.body()
        try:
            parsed = json.loads(raw)
        except json.JSONDecodeError as exc:
            return _json_400(errors.json_deserialize_error(raw, exc))
        if isinstance(parsed, dict):
            text = raw.decode("utf-8", errors="replace")
            type_error = _check_typed_fields(text, parsed)
            if type_error is not None:
                return type_error
        return parsed  # type: ignore[no-any-return]

    if "application/x-www-form-urlencoded" in content_type or "multipart/form-data" in content_type:
        form = await request.form()
        body = dict(form)
        type_error = _check_form_alternatives(body, multipart="multipart/form-data" in content_type)
        if type_error is not None:
            return type_error
        return body

    return _json_400("Unsupported content-type")


def _check_form_alternatives(body: dict[str, Any], *, multipart: bool) -> Response | None:
    """Reject a malformed ``alternatives`` field the way actix's own
    extractor does — at PARSE time, before ``check_params`` ever runs, same
    ordering as the JSON path's :func:`_check_typed_fields`. Unlike the JSON
    path, form-urlencoded and multipart produce genuinely DIFFERENT error
    shapes from each other (oracle-verified against ``alternatives=lots``):

    - ``application/x-www-form-urlencoded`` (serde_urlencoded's ``u32``
      ``FromStr`` failure): 400 ``"Parse error: invalid digit found in
      string."``
    - ``multipart/form-data`` (actix-multipart's ``MPText<u32>`` field
      failure): 400 ``"An error occurred processing field: alternatives"``

    A missing or empty value is fine (``Option<u32>`` -> ``None``, matches
    the JSON path's ``if value is None: continue``). A negative int is
    treated as a parse failure here too — Rust's actual behaviour for a
    negative value on THIS content-type is unverified (no CASE exercises
    it), the same documented simplification already noted for the JSON path
    in :func:`_check_typed_fields`.
    """
    raw = body.get("alternatives")
    if raw in (None, ""):
        return None
    try:
        if int(raw) < 0:
            raise ValueError("negative alternatives")
    except (TypeError, ValueError):
        message = (
            "An error occurred processing field: alternatives"
            if multipart
            else "Parse error: invalid digit found in string."
        )
        return _json_400(message)
    return None


def _check_typed_fields(text: str, body: dict[str, Any]) -> Response | None:
    """Reject the first field (in JSON source order) whose value doesn't
    match its Rust type — see `errors.field_spans`/`errors.serde_invalid_type_error`.
    Returns `None` when every present field type-checks.
    """
    for key, (value, _start, end) in errors.field_spans(text).items():
        if value is None:
            continue
        if key in errors.STRING_FIELDS and not isinstance(value, str):
            return _json_400(errors.serde_invalid_type_error(text, end, value, "a string"))
        if key == errors.U32_FIELD and (
            isinstance(value, bool) or not isinstance(value, int) or value < 0
        ):
            return _json_400(errors.serde_invalid_type_error(text, end, value, "u32"))
    return None


def _check_params(body: dict[str, Any], config: Config, required: list[str]) -> Response | None:
    """Port of main.rs's ``check_params``: missing-required -> api-key ->
    byte char-limit, in that exact order — verified against the oracle with
    combined-violation inputs (a bad key alongside an over-limit ``q`` 403s,
    not 400; a missing ``q`` alongside a bad api key 400s on the missing
    param, not 403). ``"q"`` must be present in ``required`` for every caller
    ('/translate': q, source, target; '/detect': q) since the char-limit
    check always reads it, mirroring Rust's ``body.q.as_ref().unwrap()``
    (safe there for the identical reason).
    """
    for key in required:
        value = body.get(key)
        if not isinstance(value, str) or not value.strip():
            return _json_400(f"Invalid request: missing {key} parameter")

    if config.api_key and body.get("api_key") != config.api_key:
        return Response(
            content={"error": "Invalid API key"},
            status_code=HTTP_403_FORBIDDEN,
            media_type="application/json",
        )

    q_value = body.get("q")
    q = q_value if isinstance(q_value, str) else ""
    q_bytes = len(q.encode("utf-8"))
    if q_bytes > config.char_limit:
        return _json_400(
            f"Invalid request: request ({q_bytes}) exceeds text limit ({config.char_limit})"
        )
    return None


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
    # plain text, no Content-Type header at all (confirmed via the dump: the
    # response has no content-type header). `{"error": ...}` does NOT apply
    # here; transcribed as observed rather than per the brief's general prose.
    return Response(
        content=b"Not found",
        status_code=HTTP_404_NOT_FOUND,
        headers={"content-type": ""},
    )


def _method_not_allowed_handler(
    request: Request[Any, Any, State], exc: MethodNotAllowedException
) -> Response:
    # Same static-file-service fallback as above, but its method guard (only
    # GET/HEAD) rejects first: actix-web-static-files' generic 405, always
    # "GET and HEAD" regardless of which route/method was actually intended.
    return Response(
        content="This resource only supports GET and HEAD.",
        status_code=HTTP_405_METHOD_NOT_ALLOWED,
        media_type="text/plain",
        headers={"allow": "GET, HEAD"},
    )


# ---------------------------------------------------------------------------
# Application factory
# ---------------------------------------------------------------------------


def create_app(config: Config) -> Litestar:
    """Build and return the Litestar application for the given config."""

    llm_client: LLMClient | None = None
    http_client: httpx.AsyncClient | None = None
    static_dir: Path = get_static_dir()

    @asynccontextmanager
    async def lifespan(app: Litestar):
        nonlocal llm_client, http_client

        logging.basicConfig(
            level=logging.DEBUG if config.verbose else logging.INFO,
            format="%(asctime)s %(name)s %(levelname)s %(message)s",
        )

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
    requests_total = Counter(
        "ltengine_requests_total",
        "Total HTTP requests",
        ["method", "path", "status"],
        registry=registry,
    )
    request_duration_seconds = Histogram(
        "ltengine_request_duration_seconds",
        "HTTP request duration in seconds",
        ["method"],
        registry=registry,
    )

    # ------------------------------------------------------------------
    # Route handlers
    # ------------------------------------------------------------------

    @get("/health", name="health")
    async def health() -> Response:
        return Response(content=None, status_code=HTTP_200_OK)

    @get("/languages", name="languages")
    async def languages() -> list[dict[str, Any]]:
        return [
            {"code": lang.code, "name": lang.name, "targets": lang.targets} for lang in LANGUAGES
        ]

    @get("/frontend/settings", name="frontend_settings")
    async def frontend_settings() -> dict[str, Any]:
        return {
            # Rust hardcodes both of these to False regardless of whether
            # --api-key is set (main.rs's get_frontend_settings) — the
            # frontend settings payload lies about auth even when an API key
            # is actually enforced server-side. Not a bug to "fix"; parity.
            "apiKeys": False,
            "charLimit": config.char_limit,
            "filesTranslation": False,
            "frontendTimeout": 1000,
            "keyRequired": False,
            "language": {
                "source": {"code": "auto", "name": "Auto Detect"},
                "target": {"code": "en", "name": "English"},
            },
            "suggestions": False,
            "supportedFilesFormat": [],
        }

    @post("/detect", name="detect")
    async def detect(request: Request[Any, Any, State]) -> Response:
        parsed = await _parse_translate_payload(request)
        if isinstance(parsed, Response):
            return parsed
        if not isinstance(parsed, dict):
            return _json_400("Invalid request body")
        body = parsed

        err = _check_params(body, config, ["q"])
        if err is not None:
            return err

        q_value = body.get("q")
        q = q_value if isinstance(q_value, str) else ""
        result = detect_language(q)
        # Key order matches Rust's `serde_json::json!({"language":...,
        # "confidence":...})`: dynamically-built `Value::Object`s serialize
        # via serde_json's default BTreeMap (alphabetical), NOT insertion
        # order — confirmed against the oracle across every multi-key
        # dynamic response body (frontend/settings, translate's result).
        return Response(
            content=[{"confidence": result.confidence, "language": result.language.code}],
            status_code=HTTP_200_OK,
            media_type="application/json",
        )

    @post("/translate", name="translate")
    async def translate(request: Request[Any, Any, State]) -> Response:
        parsed = await _parse_translate_payload(request)
        if isinstance(parsed, Response):
            return parsed
        if not isinstance(parsed, dict):
            return _json_400("Invalid request body")
        body = parsed

        # `alternatives` is typed `Option<u32>` on the Rust struct and is
        # type-checked at PARSE time for EVERY content type — JSON via
        # `_check_typed_fields`, form/multipart via `_check_form_alternatives`
        # — both called from `_parse_translate_payload` above, before
        # `check_params` ever runs. By the time control reaches here the
        # value (if present) is therefore already guaranteed to be a
        # non-negative int-parseable value; this is a plain coercion, not a
        # second validation pass.
        raw_alternatives = body.get("alternatives")
        alternatives = 0 if raw_alternatives in (None, "") else int(raw_alternatives)

        err = _check_params(body, config, ["q", "source", "target"])
        if err is not None:
            return err

        def _str_field(key: str) -> str:
            value = body.get(key)
            return value if isinstance(value, str) else ""

        q = _str_field("q")
        source = _str_field("source")
        target = _str_field("target")
        fmt = _str_field("format") or "text"

        # Format check
        fmt_error = _validate_format(fmt)
        if fmt_error:
            return _json_400(fmt_error)

        # Build prompt
        pb = PromptBuilder()
        pb.set_format(fmt)

        if source == "auto":
            pb.set_source_language("auto")
        else:
            src_lang = get_language_by_code(source)
            if src_lang is None:
                return _json_400(f"{source} is not supported")
            pb.set_source_language(src_lang.name)

        tgt_lang = get_language_by_code(target)
        if tgt_lang is None:
            return _json_400(f"{target} is not supported")
        pb.set_target_language(tgt_lang.name)

        # Rust builds EVERY /translate 200 body — identity, fast success, and
        # the slow-failure fallback alike — through one function,
        # `translate_response_json(q, translated_text, source, alternatives)`,
        # which applies `improve_formatting` itself. Mirroring that single
        # builder here is what guarantees the streamed fallback body cannot
        # drift from the success body (oracle-verified: a slow failure with
        # source="auto" still carries `detectedLanguage`, and `alternatives`
        # when requested).
        #
        # Detection is hoisted out of the builder so it runs at most once per
        # request even though the builder may be called twice (success body +
        # fallback body); it is a pure function of `q`, so this is a
        # byte-identical rearrangement of Rust's per-call `detect_lang`.
        detected = detect_language(q) if source == "auto" else None

        def _result_body(translated_text: str) -> dict[str, Any]:
            """Port of main.rs's ``translate_response_json``.

            Key order (both here and the nested detectedLanguage object)
            matches Rust's `serde_json::json!({...})` — a dynamically-built
            `Value::Object` serializes via serde_json's default BTreeMap
            (alphabetical), not insertion order: alternatives <
            detectedLanguage < translatedText, and confidence < language.
            Insert in that fixed order regardless of which optional keys end
            up present.
            """
            body: dict[str, Any] = {}

            if alternatives > 0:
                body["alternatives"] = []

            if detected is not None:
                body["detectedLanguage"] = {
                    "confidence": detected.confidence,
                    "language": detected.language.code,
                }

            body["translatedText"] = _improve_formatting(q, translated_text)
            return body

        # Identity path: no LLM call, so no grace machinery — Rust returns here
        # before the race is even set up. The formatting pass still runs
        # (`improve_formatting(&q, &q)`), so the response is not the raw
        # untouched q; it trims and re-applies q's own whitespace rules.
        if source == target:
            return Response(
                content=_result_body(q), status_code=HTTP_200_OK, media_type="application/json"
            )

        prompt = pb.build(q)
        cap = output_cap(
            len(q),
            config.llm_max_tokens,
            config.llm_chars_per_token,
            config.llm_max_tokens_mult,
            config.llm_max_tokens_floor,
        )

        # Hoisted + explicitly raised (not `assert`) so the guard survives
        # `python -O` (which strips assert statements) — an `-O` deployment
        # must still refuse to proceed with a `None` client rather than crash
        # deeper inside `work()` with an AttributeError. Binding to a fresh
        # local (`client`, not reassigning `llm_client`) also lets mypy narrow
        # `LLMClient | None` -> `LLMClient` inside the `work()` closure below.
        if llm_client is None:
            # constructed in `lifespan`, before serving
            raise RuntimeError("llm_client not initialized")
        client = llm_client

        async def work() -> dict[str, Any]:
            raw = await client.translate(prompt.system, prompt.user, max_tokens=cap)
            return _result_body(raw)

        # Grace-race (main.rs:364-411). A failure that lands INSIDE the grace
        # period propagates as an LlmError to `_llm_error_handler` below (Rust
        # taxonomy: transient upstream -> 503, other upstream statuses / parse
        # failures / empty choices -> 500). Only a failure that arrives AFTER
        # the 200 has been committed to the wire degrades to the original text
        # — Rust's wart, reproduced deliberately. See streaming.py.
        return await respond_with_grace(work(), fallback_body=_result_body(q))

    @post("/translate_file", name="translate_file")
    async def translate_file() -> Response:
        return Response(
            content={"error": "Not implemented"},
            status_code=HTTP_501_NOT_IMPLEMENTED,
            media_type="application/json",
        )

    @post("/suggest", name="suggest")
    async def suggest() -> Response:
        return Response(
            content={"error": "Not implemented"},
            status_code=HTTP_501_NOT_IMPLEMENTED,
            media_type="application/json",
        )

    @get("/metrics", name="metrics")
    async def metrics() -> Response:
        return Response(
            content=generate_latest(registry),
            status_code=HTTP_200_OK,
            media_type=CONTENT_TYPE_LATEST,
        )

    # ------------------------------------------------------------------
    # Metrics middleware (track requests)
    # ------------------------------------------------------------------

    class MetricsMiddleware:
        """Track HTTP requests and responses with Prometheus metrics."""

        def __init__(self, app: ASGIApp) -> None:
            self.app = app

        async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
            if scope["type"] != "http":
                await self.app(scope, receive, send)
                return

            import time

            method: str = scope.get("method", "UNKNOWN")  # type: ignore[assignment]
            path: str = scope.get("path", "/")  # type: ignore[assignment]
            start_time = time.perf_counter()
            status_code = 500  # default to 500 if an exception occurs

            async def send_with_metrics(message: dict[str, Any]) -> None:  # type: ignore[name-defined]
                nonlocal status_code
                if message["type"] == "http.response.start":
                    status_code = message["status"]
                await send(message)  # type: ignore[arg-type]

            try:
                await self.app(scope, receive, send_with_metrics)  # type: ignore[arg-type]
            finally:
                # Record metrics
                duration = time.perf_counter() - start_time
                requests_total.labels(method=method, path=path, status=status_code).inc()
                request_duration_seconds.labels(method=method).observe(duration)

    # ------------------------------------------------------------------
    # Static file middleware (serves frontend UI)
    # ------------------------------------------------------------------

    class StaticFilesMiddleware:
        """Serve static files for any path not matched by an API route."""

        def __init__(self, app: ASGIApp) -> None:
            self.app = app

        async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
            if scope["type"] != "http":
                await self.app(scope, receive, send)
                return

            path: str = scope.get("path", "/")
            # API routes handled by Litestar — only catch root and unknown paths
            api_prefixes = (
                "/translate",
                "/detect",
                "/languages",
                "/health",
                "/frontend",
                "/suggest",
                "/metrics",
            )
            if path == "/" or not any(path.startswith(p) for p in api_prefixes):
                # Serve index.html for root; for everything else try the file
                if path == "/":
                    file_path = static_dir / "index.html"
                else:
                    candidate = static_dir / path.lstrip("/")
                    file_path = candidate if candidate.exists() else static_dir / "index.html"

                if file_path.exists():
                    content = file_path.read_bytes()
                    mime = _guess_mime(file_path.suffix)
                    response = Response(
                        content=content,
                        media_type=mime,
                        status_code=200,
                    )
                    await response(scope, receive, send)  # type: ignore[operator]
                    return

            await self.app(scope, receive, send)

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

    return Litestar(
        route_handlers=[
            health,
            languages,
            frontend_settings,
            detect,
            translate,
            translate_file,
            suggest,
            metrics,
        ],
        middleware=[MetricsMiddleware, StaticFilesMiddleware],
        lifespan=[lifespan],
        debug=config.verbose,
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
    Granian's `target` at `"ltengine.app:app_factory"` with `factory=True`.
    """
    from .config import Config

    return create_app(Config.from_env())
