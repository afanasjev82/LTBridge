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
from typing import Any

import httpx

# ---------------------------------------------------------------------------
# LLM upstream failure taxonomy — port of llm_api.rs::ApiError
# ---------------------------------------------------------------------------

#: Upstream statuses treated as transient/retryable. Port of
#: ``matches!(*status, 502 | 503 | 504 | 429)`` in ``ApiError::http_status``.
TRANSIENT_UPSTREAM = {429, 502, 503, 504}


class LlmError(Exception):
    """Raised by :class:`~ltbridge.llm_client.LLMClient` on any upstream failure.

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


#: The four bytes serde_json's ``parse_whitespace`` skips.
_JSON_WS = b" \t\n\r"


def byte_position(raw: bytes, index: int) -> tuple[int, int]:
    """serde_json's ``Position`` for a 0-indexed BYTE offset into the request body.

    Port of ``SliceRead::position_of_index``: the line is 1-indexed, the column
    is the number of BYTES consumed since that line began (so column 0 is
    "nothing consumed on this line yet", not "the first character"). Every
    serde_json error position in this module funnels through here — see
    :func:`json_deserialize_error` for how ``index`` itself is derived.

    BYTES, not characters, is the whole point: serde_json indexes the raw
    request body, so a multi-byte character before the failure advances the
    column by its encoded length. CPython's ``json`` counts characters, which
    silently under-reports the column on any non-ASCII body — oracle-verified
    divergence (``{"q":"привет","source" "en"}`` is column 30 in Rust, 24 by
    CPython's character count).
    """
    prefix = raw[:index]
    newlines = prefix.count(b"\n")
    if newlines:
        return newlines + 1, index - prefix.rindex(b"\n") - 1
    return 1, index


def _byte_offset(text: str, char_offset: int) -> int:
    """Byte offset of ``char_offset`` in the body ``text`` was decoded from.

    ``surrogateescape`` on both legs is a byte-exact round trip (see
    :func:`json_deserialize_error`), so this converts CPython's character-based
    ``JSONDecodeError.pos`` into the byte offset serde_json counts in.
    """
    return len(text[:char_offset].encode("utf-8", errors="surrogateescape"))


def _skip_ws_bytes(raw: bytes, index: int) -> int:
    """First non-whitespace BYTE at or after ``index`` (``len(raw)`` if none).

    Distinct from :func:`_skip_ws` further down, which walks a decoded ``str``
    for :func:`field_spans`; this one indexes the raw body, because serde's
    positions are byte offsets.
    """
    n = len(raw)
    while index < n and raw[index] in _JSON_WS:
        index += 1
    return index


def json_deserialize_error(
    raw: bytes, exc: json.JSONDecodeError, text: str | None = None
) -> str:
    """Build the ``{"error": ...}`` message for a malformed JSON request body.

    Best-effort port of serde_json's ``Display for ErrorImpl`` (``"{code} at
    line {line} column {column}"``), driven off CPython's ``json`` diagnostics.
    See the module-level notes above on the one verified case vs the rest.

    ``text`` is the string ``exc`` was actually raised against; it defaults to
    ``raw`` decoded with ``errors="surrogateescape"``. That codec is not
    cosmetic — it is a byte-EXACT round trip (each undecodable byte becomes one
    lone surrogate that re-encodes to the same single byte), which is what lets
    ``exc.pos``, a character offset, be converted back to serde's byte offset
    for ANY body, valid UTF-8 or not. ``errors="replace"`` (what this used
    before v0.1.3) collapses a multi-byte invalid sequence into one U+FFFD and
    silently shifts every offset after it.

    **Position rule.** serde_json reports ``position_of_index(self.index)``,
    and ``self.index`` sits just PAST what it consumed:

    - EOF-classified codes -> ``index = len(raw)`` ("bytes consumed"). An empty
      body is column 0, ``{"a":`` is column 5, and an unterminated string runs
      to the end of the body (oracle: ``{"q":"ab`` is column 8, NOT the opening
      quote CPython points at).
    - an escape -> serde has consumed the whole escape sequence: ``\\X`` is two
      bytes past the backslash, ``\\uXXXX`` six (oracle: ``{"q":"a\\zb"}`` is
      column 9, ``{"q":"a\\uZZZZb"}`` column 13 — and 13 for a bad digit in ANY
      of the four positions, so the four are always consumed first).
    - everything else -> ``index = <offending byte> + 1``. CPython's ``pos`` is
      that byte for most messages, but the ``Expecting …`` family stops BEFORE
      skipping whitespace where serde skips it first, so those advance past
      whitespace to find the byte serde actually complained about (oracle:
      ``{"q" "x"}`` is column 6, not 5). The control-character branch must NOT
      skip — there the whitespace IS the offending byte (oracle: a raw newline
      inside a string reports line 2 column 0).
    """
    if text is None:
        text = raw.decode("utf-8", errors="surrogateescape")
    at_eof = exc.pos >= len(text)
    is_eof_code = False
    # Bytes serde consumes past `exc.pos` before reporting; 1 == "the offending
    # byte itself" and is the norm.
    consumed = 1

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
        # CPython points at the OPENING quote; serde scans to end-of-input
        # looking for the closing one and reports from there — so this is an
        # EOF-family position despite `exc.pos` being nowhere near the end.
        # Oracle-verified (`{"q":"ab` -> column 8; `{\n"q":"ab` -> line 2
        # column 7).
        code = "EOF while parsing a string"
        is_eof_code = True
    elif exc.msg.startswith("Invalid control character"):
        code = "control character (\\u0000-\\u001F) found while parsing a string"
    elif exc.msg.startswith("Invalid \\uXXXX escape"):
        # CPython's `pos` is the "u"; serde reads it plus four hex digits.
        code = "invalid escape"
        consumed = 5
    elif exc.msg.startswith("Invalid \\escape"):
        # CPython's `pos` is the backslash; serde reads it plus the escape char.
        code = "invalid escape"
        consumed = 2
    elif exc.msg.startswith("Unexpected UTF-8 BOM"):
        # CPython special-cases a leading U+FEFF with its own message; serde
        # has no concept of a BOM and simply meets a byte that starts no value.
        # Oracle-verified: column 1.
        code = "expected value"
    else:
        # Unmapped CPython message — not a transcribed Rust string; best-effort
        # passthrough so the response is still a taxonomically-correct 400.
        code = exc.msg

    if is_eof_code:
        index = len(raw)
    else:
        offending = _byte_offset(text, exc.pos)
        if exc.msg.startswith("Expecting"):
            offending = _skip_ws_bytes(raw, offending)
        index = offending + consumed

    line, column = byte_position(raw, index)
    return f"Json deserialize error: {code} at line {line} column {column}"


# ---------------------------------------------------------------------------
# Invalid UTF-8 request bodies — serde_json validates UTF-8 LAZILY, only for
# the contents of a string token, and only once that token's closing quote has
# been consumed. Everything outside a string is scanned as raw bytes, so a
# stray ``0xFF`` there is just an unexpected byte and produces an ordinary
# syntax error instead. CPython's `json` module does the opposite — it decodes
# the WHOLE body up front — so `json.loads(<invalid utf-8>)` raises
# `UnicodeDecodeError`, which is NOT a `JSONDecodeError` and, before v0.1.3,
# escaped `_parse_translate_payload` entirely: Litestar answered
# `{"status_code":500,"detail":"Internal Server Error"}` where Rust answers a
# 400. The functions below reproduce serde's laziness.
# ---------------------------------------------------------------------------

_QUOTE = 0x22
_BACKSLASH = 0x5C


def _first_invalid_utf8(raw: bytes) -> int | None:
    """Offset of the first byte that is not part of valid UTF-8, else ``None``.

    A deliberate STRICT decode, independent of however the caller failed:
    ``json.loads(bytes)`` runs ``detect_encoding`` first (so a UTF-16/32 BOM
    sends it down a completely different decode) and then decodes with
    ``surrogatepass`` (so CESU-8 surrogate bytes sail through, where Rust
    rejects them). Neither is serde's rule; this is.
    """
    try:
        raw.decode("utf-8")
    except UnicodeDecodeError as exc:
        return exc.start
    return None


def _closed_string_end(raw: bytes, bad: int) -> int | None:
    """Index one byte past the closing quote of the string token containing ``bad``.

    ``None`` when serde would not reach a UTF-8 error at all: ``bad`` sits
    outside any string token, or inside a string that is never closed (serde
    hits end-of-input first and reports ``EOF while parsing a string``), or is
    itself the byte an escape introduces (``invalid escape`` wins). In each of
    those cases the caller falls back to the ordinary syntax path.

    A plain string tokenizer, not a parser: serde only needs the closing quote
    to decide where to report, and the caller's syntax-error comparison covers
    the cases where the document is malformed enough for the two views of
    "which token is this" to disagree.
    """
    i = 0
    n = len(raw)
    while i < n:
        if raw[i] != _QUOTE:
            if i >= bad:
                return None  # reached `bad` outside any string token
            i += 1
            continue
        j = i + 1
        while j < n:
            if raw[j] == _BACKSLASH:
                if j + 1 == bad:
                    return None  # `bad` IS the escaped byte -> "invalid escape"
                j += 2
                continue
            if raw[j] == _QUOTE:
                break
            j += 1
        if i < bad < j:
            return None if j >= n else j + 1
        if j >= n:
            return None  # unterminated string, and `bad` is not inside it
        i = j + 1
    return None


def invalid_utf8_error(raw: bytes) -> str:
    """Build the ``{"error": ...}`` message for a body that is not valid UTF-8.

    Reproduces serde_json's lazy validation: if the first invalid byte lands
    inside a CLOSED string token, the error is ``invalid unicode code point``
    positioned one byte past that token's closing quote (serde validates the
    slice only after consuming the whole token — oracle-verified, including a
    ~4.6 KiB Cyrillic body corrupted at byte 100, which reports column 4584,
    i.e. the end of ``q``, not the corruption site). Otherwise serde meets the
    byte as an ordinary syntax error, so the message is built by
    :func:`json_deserialize_error` over a ``surrogateescape`` decode — a
    byte-exact round trip, so the offsets stay byte-exact and each invalid byte
    stays one ordinary character rather than collapsing the way
    ``errors="replace"`` would.

    A syntax error EARLIER in the document wins, because serde would hit it
    before ever finishing the offending string token: the comparison is
    against the string's CLOSING quote, since a fault INSIDE the token (a raw
    control character, say) also pre-empts the UTF-8 check.
    """
    bad = _first_invalid_utf8(raw)
    if bad is None:  # pragma: no cover - callers only reach this on a failed decode
        bad = len(raw)

    end = _closed_string_end(raw, bad)
    text = raw.decode("utf-8", errors="surrogateescape")
    syntax_exc: json.JSONDecodeError | None = None
    try:
        json.loads(text)
    except json.JSONDecodeError as exc:
        syntax_exc = exc

    if end is not None and (syntax_exc is None or _byte_offset(text, syntax_exc.pos) >= end):
        line, column = byte_position(raw, end)
        return f"Json deserialize error: invalid unicode code point at line {line} column {column}"

    if syntax_exc is not None:
        return json_deserialize_error(raw, syntax_exc, text=text)

    # Unreachable in practice (a body with an invalid byte outside a string
    # cannot also parse cleanly), but a request must never fall off the end of
    # this function into a 500 — that is the bug being fixed.
    line, column = byte_position(raw, bad + 1)  # pragma: no cover
    return (  # pragma: no cover
        f"Json deserialize error: invalid unicode code point at line {line} column {column}"
    )


# ---------------------------------------------------------------------------
# Typed-field mismatches — port of serde's derived struct Deserialize for
# `TranslateRequest` (main.rs: `q`/`source`/`target`/`format`/`api_key` are
# `Option<String>`, `alternatives` is `Option<u32>`). Rust parses the JSON
# body into this TYPED struct before any handler logic runs (`parse_payload`
# -> `web::Json::<TranslateRequest>::from_request`), so a present-but-wrong-
# typed field (e.g. `"q":123`) is rejected by serde as a 400 "Json
# deserialize error: invalid type: ..." — the same message FAMILY as
# `json_deserialize_error` above (both surface through actix's
# `JsonPayloadError::Deserialize(serde_json::Error)` -> Display), but the
# underlying `serde_json::Error` is a Data-category error (from
# `serde::de::Error::invalid_type`), not Syntax/Eof, so the code text is
# built from the VALUE's own JSON kind rather than CPython's json module
# diagnostics — hence a separate code path here rather than reusing
# `_NON_EOF_CODE`/`_EOF_CODE`.
# ---------------------------------------------------------------------------

#: Fields on TranslateRequest typed `Option<String>` — a present, non-null
#: value must be a JSON string or the request 400s before any handler-level
#: (`check_params`) validation runs.
STRING_FIELDS = ("q", "source", "target", "format", "api_key")
#: The one non-string field, typed `Option<u32>`.
U32_FIELD = "alternatives"

_decoder = json.JSONDecoder()


def _skip_ws(text: str, i: int) -> int:
    n = len(text)
    while i < n and text[i] in " \t\n\r":
        i += 1
    return i


def field_spans(text: str) -> dict[str, tuple[Any, int, int]]:
    """Parse a top-level JSON object, returning each key's ``(value, start,
    end)`` token span (0-indexed char offsets into ``text``) in JSON SOURCE
    ORDER — the same order serde's derived ``Deserialize`` visits map entries
    in, which matters because a wrong-typed field fails at the FIRST one
    encountered in the input, not in struct-declaration order.

    Returns ``{}`` for a non-object top level (arrays/scalars have no named
    fields to type-check) or on any parse hiccup. ``text`` is assumed to
    already be valid JSON — the caller only invokes this after ``json.loads``
    has already succeeded on the same bytes — so this is a best-effort
    re-walk (reusing :class:`json.JSONDecoder`'s own tokenizer via
    ``raw_decode`` for each key/value), not a general parser; any surprise is
    swallowed rather than crashing a request that already parsed cleanly.
    """
    spans: dict[str, tuple[Any, int, int]] = {}
    try:
        n = len(text)
        i = _skip_ws(text, 0)
        if i >= n or text[i] != "{":
            return spans
        i = _skip_ws(text, i + 1)
        if i < n and text[i] == "}":
            return spans
        while True:
            i = _skip_ws(text, i)
            key, i = _decoder.raw_decode(text, i)
            i = _skip_ws(text, i) + 1  # step past ':'
            i = _skip_ws(text, i)
            start = i
            value, i = _decoder.raw_decode(text, i)
            spans[str(key)] = (value, start, i)
            i = _skip_ws(text, i)
            if i < n and text[i] == ",":
                i += 1
                continue
            break
    except (ValueError, IndexError):
        return {}
    return spans


def _line_col(text: str, offset: int) -> tuple[int, int]:
    """serde_json line/column for a 0-indexed CHARACTER offset into ``text``.

    Thin adapter over :func:`byte_position`, which is the single implementation
    of serde_json's position rule: 1-indexed line, column = BYTES consumed
    since that line began. ``offset`` arrives as a character offset (it comes
    from :func:`field_spans`, which walks the decoded string), so it is encoded
    to a byte offset first — the two only coincide on an ASCII body, and the
    oracle counts bytes. Oracle-verified byte-for-byte: ``{"q":123}`` ->
    column 8, ``{"q":"x","source":123,"target":"ru"}`` -> column 21, and a
    garbage ``alternatives`` string value -> column 62.
    """
    data = text.encode("utf-8", errors="surrogateescape")
    return byte_position(data, _byte_offset(text, offset))


def _unexpected_desc(value: Any) -> str:
    """Port of serde's ``Unexpected`` Display — the noun phrase naming what
    WAS found, e.g. ``integer `123``` or ``string "lots"``. Covers the JSON
    value kinds that can actually appear in a TranslateRequest body; ``bool``
    is checked before ``int`` since Python's ``bool`` subclasses ``int``.
    """
    if isinstance(value, bool):
        return f"boolean `{'true' if value else 'false'}`"
    if isinstance(value, int):
        return f"integer `{value}`"
    if isinstance(value, float):
        return f"floating point `{value}`"
    if isinstance(value, str):
        return f"string {json.dumps(value)}"
    if isinstance(value, list):
        return "sequence"
    if isinstance(value, dict):
        return "map"
    return "null"


def serde_invalid_type_error(text: str, end: int, value: Any, expected: str) -> str:
    """Build the ``{"error": ...}`` message for a typed-field mismatch — the
    Data-error analogue of :func:`json_deserialize_error`. ``end`` is the
    value's span end from :func:`field_spans`; ``expected`` is the Rust type
    noun phrase (``"a string"`` for the ``Option<String>`` fields, ``"u32"``
    for ``alternatives``).
    """
    line, col = _line_col(text, end)
    desc = _unexpected_desc(value)
    return f"Json deserialize error: invalid type: {desc}, expected {expected} at line {line} column {col}"
