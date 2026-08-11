"""Error taxonomy — port of llm_api.rs's ``ApiError`` + error_response.rs.

Every message string below is transcribed VERBATIM from the observed Rust
reference binary (``.rust-ref/target/release/ltengine.exe``) — captured via
``scripts/dump_rust_responses.py`` and recorded in
``.superpowers/sdd/task-6-report.md``. Do not hand-edit these strings; if the
Rust reference changes, re-run the dump against a rebuilt binary and update
both places together.
"""

from __future__ import annotations

import json

import httpx

# ---------------------------------------------------------------------------
# LLM upstream failure taxonomy — port of llm_api.rs::ApiError
# ---------------------------------------------------------------------------

#: Upstream statuses treated as transient/retryable. Port of
#: ``matches!(*status, 502 | 503 | 504 | 429)`` in ``ApiError::http_status``.
TRANSIENT_UPSTREAM = {429, 502, 503, 504}


class LlmError(Exception):
    """Raised by :class:`~ltengine.llm_client.LLMClient` on any upstream failure.

    Carries the exact HTTP status + message the app-level exception handler
    emits as ``{"error": message}`` — the Python analogue of Rust's
    ``ErrorResponse { error, status }``.
    """

    def __init__(self, http_status: int, message: str) -> None:
        super().__init__(message)
        self.http_status = http_status
        self.message = message


def unreachable(detail: str) -> LlmError:
    """Port of ``ApiError::Unreachable(String)`` — Display: ``"LLM API unreachable: {0}"``."""
    return LlmError(503, f"LLM API unreachable: {detail}")


def timeout() -> LlmError:
    """Port of ``ApiError::Timeout`` — a unit variant; Rust's Display carries no detail.

    Observed text: ``"LLM API request timed out"`` (no interpolated detail —
    the original task-brief skeleton's ``f"LLM API timed out: {detail}"`` does
    not match the oracle and was corrected).
    """
    return LlmError(503, "LLM API request timed out")


def remote_status(status: int, body: str) -> LlmError:
    """Port of ``ApiError::Remote { status, body }`` —
    Display: ``"LLM API returned status {status}: {body}"``.

    ``body`` is embedded verbatim (the raw upstream response text), matching
    Rust's ``response.text().await.unwrap_or_default()``.
    """
    http_status = 503 if status in TRANSIENT_UPSTREAM else 500
    return LlmError(http_status, f"LLM API returned status {status}: {body}")


def bad_response(detail: str) -> LlmError:
    """Port of ``ApiError::Parse(String)`` — Display: ``"LLM API response parse error: {0}"``."""
    return LlmError(500, f"LLM API response parse error: {detail}")


def empty_choices() -> LlmError:
    """Port of ``ApiError::NoChoices`` — Display: ``"LLM API returned no choices"``."""
    return LlmError(500, "LLM API returned no choices")


def map_llm_failure(exc_or_status: Exception | tuple[int, str]) -> LlmError:
    """Map an httpx transport exception, or an ``(upstream_status, body)`` pair, to an
    :class:`LlmError`.

    Mirrors ``LLM::run_prompt``'s ``req.send().await.map_err(|e| ...)`` dispatch
    exactly: *any* timeout-family exception -> :func:`timeout`; every other
    transport failure (connect refused, DNS, protocol errors, ...) ->
    :func:`unreachable` — i.e. Rust's
    ``if e.is_timeout() { ApiError::Timeout } else { ApiError::Unreachable(e.to_string()) }``.
    reqwest's ``is_timeout()`` is true for a connect timeout too, so
    ``httpx.ConnectTimeout`` (a ``TimeoutException`` subclass) maps to
    :func:`timeout`, not :func:`unreachable` — the task brief's prose suggesting
    otherwise does not match the oracle.

    An ``(status, body)`` tuple is a convenience for the post-response
    remote-status branch and simply delegates to :func:`remote_status`.
    """
    if isinstance(exc_or_status, tuple):
        status, body = exc_or_status
        return remote_status(status, body)
    if isinstance(exc_or_status, httpx.TimeoutException):
        return timeout()
    return unreachable(str(exc_or_status))


# ---------------------------------------------------------------------------
# Request-body JSON syntax errors — port of serde_json's Display + actix's
# ``web::Json`` extractor -> ``ErrorResponse::from(actix_web::Error)`` mapping
# (error_response.rs). Rust: ``{"error": "Json deserialize error: <code> at
# line <L> column <C>"}``, HTTP 400.
# ---------------------------------------------------------------------------

#: CPython's ``json`` module message -> serde_json ``ErrorCode`` Display text.
#: Transcribed from serde_json 1.0.149's ``error.rs`` (the version pinned in
#: ``.rust-ref/Cargo.lock``). Verified byte-for-byte against the Rust oracle
#: ONLY for the ``"key must be a string"`` case (the one committed differential
#: test, ``{not json``) — CPython's ``json`` module happens to compute the same
#: 1-indexed line/column as serde_json for that input. The remaining entries are
#: a faithful transcription of serde_json's source but are NOT independently
#: verified against the live binary; treat them as best-effort, not oracle-grade.
_NON_EOF_CODE = {
    "Expecting property name enclosed in double quotes": "key must be a string",
    "Expecting value": "expected value",
    "Expecting ':' delimiter": "expected `:`",
    "Extra data": "trailing characters",
}
_EOF_CODE = {
    "Expecting property name enclosed in double quotes": "EOF while parsing an object",
    "Expecting value": "EOF while parsing a value",
    "Expecting ':' delimiter": "EOF while parsing an object",
}


def _enclosing_container(text: str, pos: int) -> str:
    """Return ``"["`` or ``"{"`` for the nearest still-open bracket before ``pos``.

    CPython's ``json`` module reports the same ``"Expecting ',' delimiter"``
    message whether the comma was missing inside a list or an object; serde_json
    distinguishes ``expected `,` or `]``` vs ``expected `,` or `}```. This
    walks the text up to the failure position, tracking bracket nesting while
    skipping over string contents (including escapes), to recover that context.
    """
    stack: list[str] = []
    in_string = False
    escape = False
    for ch in text[:pos]:
        if in_string:
            if escape:
                escape = False
            elif ch == "\\":
                escape = True
            elif ch == '"':
                in_string = False
            continue
        if ch == '"':
            in_string = True
        elif ch in "{[":
            stack.append(ch)
        elif ch in "}]" and stack:
            stack.pop()
    return stack[-1] if stack else "{"


def json_deserialize_error(raw: bytes, exc: json.JSONDecodeError) -> str:
    """Build the ``{"error": ...}`` message for a malformed JSON request body.

    Best-effort port of serde_json's ``Display for ErrorImpl`` (``"{code} at
    line {line} column {column}"``), driven off CPython's ``json`` diagnostics.
    See the module-level notes above on the one verified case vs the rest.

    Column at EOF: serde_json's EOF-family errors report ``column = bytes
    consumed`` (0-indexed byte count) — e.g. an empty body is column 0, a
    5-byte truncated body is column 5 — whereas CPython's ``colno`` is
    ``pos + 1`` (1-indexed). Verified against the oracle (Fix wave 2, empty
    body and ``{"a":``): every EOF-classified branch below must subtract 1
    from ``exc.colno``; non-EOF branches are unaffected (the verified
    ``{not json`` case must keep passing byte-for-byte).
    """
    text = raw.decode("utf-8", errors="replace")
    at_eof = exc.pos >= len(text)
    is_eof_code = False

    if at_eof and exc.msg in _EOF_CODE:
        code = _EOF_CODE[exc.msg]
        is_eof_code = True
    elif exc.msg == "Expecting ',' delimiter":
        container = _enclosing_container(text, exc.pos)
        if at_eof:
            code = "EOF while parsing a list" if container == "[" else "EOF while parsing an object"
            is_eof_code = True
        else:
            code = "expected `,` or `]`" if container == "[" else "expected `,` or `}`"
    elif exc.msg in _NON_EOF_CODE:
        code = _NON_EOF_CODE[exc.msg]
    elif exc.msg.startswith("Unterminated string starting at"):
        # NOT adjusted: CPython reports the position of the OPENING quote here,
        # not end-of-input, so `at_eof`/the bytes-consumed rule don't apply the
        # same way — this remains unverified best-effort (see module docstring).
        code = "EOF while parsing a string"
    elif exc.msg.startswith("Invalid control character"):
        code = "control character (\\u0000-\\u001F) found while parsing a string"
    elif exc.msg.startswith("Invalid \\escape") or exc.msg.startswith("Invalid \\uXXXX escape"):
        code = "invalid escape"
    else:
        # Unmapped CPython message — not a transcribed Rust string; best-effort
        # passthrough so the response is still a taxonomically-correct 400.
        code = exc.msg

    colno = exc.colno - 1 if is_eof_code else exc.colno
    return f"Json deserialize error: {code} at line {exc.lineno} column {colno}"
