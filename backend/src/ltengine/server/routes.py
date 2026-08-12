"""API route handlers for the LibreTranslate-compatible surface.

Extracted from app.py's route-handler closures (Task 12, pure move): every
handler that answers a public endpoint — ``/health``, ``/languages``,
``/frontend/settings``, ``/detect``, ``/translate``, ``/translate_file``,
``/suggest`` — lives here, built by :func:`create_router`. ``/metrics`` stays
in ``server/app.py`` alongside the Prometheus registry it reads from.
"""

from __future__ import annotations

import json
from typing import Any

from litestar import Request, Response, Router, get, post
from litestar.datastructures import State
from litestar.status_codes import (
    HTTP_200_OK,
    HTTP_400_BAD_REQUEST,
    HTTP_403_FORBIDDEN,
    HTTP_501_NOT_IMPLEMENTED,
)

from ..config import Config
from ..core import errors
from ..core.formatting import improve_formatting
from ..core.languages import LANGUAGES, detect_language, get_language_by_code
from ..core.prompt import PromptBuilder
from ..core.token_budget import output_cap
from ..llm_client import LLMClient
from .streaming import respond_with_grace

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


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


def _field_type_mismatch(key: str, value: Any) -> bool:
    """True iff ``value`` doesn't match ``key``'s Rust type — the single source of
    truth for the per-field rule both :func:`_has_type_mismatch` (fast, no error
    detail) and :func:`_check_typed_fields` (precise, builds the error message) key
    off, so the rule can't drift between the two passes. ``None`` never mismatches
    (every field is ``Option<T>``). ``STRING_FIELDS`` and ``U32_FIELD`` are disjoint,
    so at most one of the two conditions can ever fire for a given key.
    """
    if value is None:
        return False
    if key in errors.STRING_FIELDS and not isinstance(value, str):
        return True
    return key == errors.U32_FIELD and (
        isinstance(value, bool) or not isinstance(value, int) or value < 0
    )


def _has_type_mismatch(body: dict[str, Any]) -> bool:
    """Cheap type check against the already-parsed body — no re-walk of the JSON
    source. Same per-field rule (:func:`_field_type_mismatch`) as
    :func:`_check_typed_fields`'s precise pass.
    """
    return any(_field_type_mismatch(key, value) for key, value in body.items())


def _check_typed_fields(text: str, body: dict[str, Any]) -> Response | None:
    """Reject the first field (in JSON source order) whose value doesn't
    match its Rust type — see `errors.field_spans`/`errors.serde_invalid_type_error`.
    Returns `None` when every present field type-checks.

    ``body`` is the SAME dict ``json.loads(text-as-bytes)`` already produced by the
    caller — its key order and values are therefore already identical to what
    ``errors.field_spans(text)`` would re-derive by walking ``text`` a second time
    (both go through CPython's json decoder over the same bytes; Python dicts
    preserve JSON source/insertion order either way, including the last-value-wins
    behaviour of a duplicate key). ``_has_type_mismatch`` exploits that to skip the
    expensive span-tracking re-parse entirely on the overwhelmingly common
    well-typed request — ``field_spans`` only runs when there's an actual mismatch
    to build a precise (span-based) error message for, which is the one thing the
    fast check can't produce on its own.
    """
    if not _has_type_mismatch(body):
        return None
    for key, (value, _start, end) in errors.field_spans(text).items():
        if not _field_type_mismatch(key, value):
            continue
        expected = "a string" if key in errors.STRING_FIELDS else "u32"
        return _json_400(errors.serde_invalid_type_error(text, end, value, expected))
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
# Router factory
# ---------------------------------------------------------------------------


def create_router(config: Config, llm_client: LLMClient) -> Router:
    """Build the ``Router`` housing every public API route handler.

    ``config`` and ``llm_client`` are closed over by the handlers below (plain
    closure DI, matching the rest of this codebase — no Litestar ``Provide``
    machinery). ``llm_client`` is a live object whose ``.model`` may still be
    filled in later by ``server/app.py``'s lifespan (``resolve_model()``); the
    same instance is captured here, so that later mutation is visible to every
    handler without any additional wiring.
    """

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

            body["translatedText"] = improve_formatting(q, translated_text)
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

        async def work() -> dict[str, Any]:
            raw = await llm_client.translate(prompt.system, prompt.user, max_tokens=cap)
            return _result_body(raw)

        # Grace-race (main.rs:364-411). A failure that lands INSIDE the grace
        # period propagates as an LlmError to `_llm_error_handler` (server/app.py
        # exception handler; Rust taxonomy: transient upstream -> 503, other
        # upstream statuses / parse failures / empty choices -> 500). Only a
        # failure that arrives AFTER the 200 has been committed to the wire
        # degrades to the original text — Rust's wart, reproduced deliberately.
        # See streaming.py.
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

    return Router(
        path="/",
        route_handlers=[
            health,
            languages,
            frontend_settings,
            detect,
            translate,
            translate_file,
            suggest,
        ],
    )
