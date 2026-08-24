import json
from typing import Any

import pytest

from .helpers import assert_parity, fire_both

B = {"q": "Hello", "source": "en", "target": "ru"}


CASES = [
    ("missing_q", "POST", "/translate", {"json_body": {"source": "en", "target": "ru"}}),
    ("blank_q", "POST", "/translate", {"json_body": {**B, "q": "   "}}),
    ("missing_source", "POST", "/translate", {"json_body": {"q": "x", "target": "ru"}}),
    ("missing_target", "POST", "/translate", {"json_body": {"q": "x", "source": "en"}}),
    ("bad_format", "POST", "/translate", {"json_body": {**B, "format": "pdf"}}),
    ("unknown_source", "POST", "/translate", {"json_body": {**B, "source": "xx"}}),
    ("unknown_target", "POST", "/translate", {"json_body": {**B, "target": "xx"}}),
    (
        "alias_source",
        "POST",
        "/translate",
        {"json_body": {**B, "source": "zh-Hans", "target": "en"}},
    ),
    ("form_urlencoded", "POST", "/translate", {"form": B}),
    ("multipart", "POST", "/translate", {"files": {k: (None, v) for k, v in B.items()}}),
    (
        "unsupported_content_type",
        "POST",
        "/translate",
        {"headers": {"content-type": "text/plain"}, "content": b"q=x"},
    ),
    ("alternatives_zero", "POST", "/translate", {"json_body": {**B, "alternatives": 0}}),
    ("alternatives_two", "POST", "/translate", {"json_body": {**B, "alternatives": 2}}),
    ("alternatives_garbage", "POST", "/translate", {"json_body": {**B, "alternatives": "lots"}}),
    (
        "identity_translation",
        "POST",
        "/translate",
        {"json_body": {"q": "  Hello  ", "source": "en", "target": "en"}},
    ),
    (
        "byte_limit_multibyte",
        "POST",
        "/translate",
        {"json_body": {"q": "я" * 2600, "source": "ru", "target": "en"}},
    ),  # 5200 bytes > 5000
    ("detect_ok", "POST", "/detect", {"json_body": {"q": "Hello world"}}),
    ("detect_missing_q", "POST", "/detect", {"json_body": {}}),
    ("detect_over_limit", "POST", "/detect", {"json_body": {"q": "я" * 2600}}),
    ("frontend_settings", "GET", "/frontend/settings", {}),
    ("translate_file_501", "POST", "/translate_file", {"json_body": {}}),
    ("suggest_501", "POST", "/suggest", {"json_body": {}}),
    # --- Inherited from Task 6's review: TYPED-FIELD parity. Rust parses the
    # request into a typed `TranslateRequest` struct — a wrong-typed field is
    # a serde 400 BEFORE any handler logic runs (check_params, format check,
    # etc. never execute). See errors.py's `field_spans`/`serde_invalid_type_error`.
    ("q_wrong_type", "POST", "/translate", {"json_body": {"q": 123}}),
    (
        "source_wrong_type",
        "POST",
        "/translate",
        {"json_body": {"q": "x", "source": 123, "target": "ru"}},
    ),
    # target: null on an Option<String> field is VALID for serde (-> None),
    # so this is NOT a type error — it falls through to the normal
    # missing-target check_params error, same as an absent "target" key.
    (
        "target_null",
        "POST",
        "/translate",
        {"json_body": {"q": "x", "source": "en", "target": None}},
    ),
    # Genuine wrong-typed `target` (distinct from target_null above): a
    # present, non-null, non-string value on the Option<String> field IS a
    # serde type error — same family as q_wrong_type/source_wrong_type.
    (
        "target_wrong_type",
        "POST",
        "/translate",
        {"json_body": {"q": "x", "source": "en", "target": 123}},
    ),
    # --- Combined-violation ordering: missing-param wins over the (separate,
    # later) format check — matches Rust's check_params-then-check_format
    # sequencing exactly.
    (
        "missing_q_and_bad_format",
        "POST",
        "/translate",
        {"json_body": {"source": "en", "target": "ru", "format": "pdf"}},
    ),
    # --- Form/multipart typed-field parity: the app.py comment claiming
    # form/multipart "has no wire-level type system, so no typed-field check
    # applies" is FALSE per main.rs — the Form path deserializes via
    # actix_web::web::Form<TranslateRequest> (serde_urlencoded,
    # alternatives: Option<u32>) and multipart via
    # MultipartForm<MPTranslateRequest> (alternatives: Option<MPText<u32>>),
    # so a malformed `alternatives` fails at the actix EXTRACTOR (a
    # different error shape) before check_params ever runs, exactly as the
    # JSON path fails at serde before check_params.
    (
        "alternatives_garbage_form",
        "POST",
        "/translate",
        {"form": {**B, "alternatives": "lots"}},
    ),
    (
        "alternatives_garbage_multipart",
        "POST",
        "/translate",
        {"files": {k: (None, v) for k, v in {**B, "alternatives": "lots"}.items()}},
    ),
    # --- Final-review IMPORTANT 3: content-type gate is case-SENSITIVE on
    # the raw header value (Rust: `content_type.starts_with("application/
    # json")`), not the lowercased value Litestar's own `request.content_type`
    # property would give. Oracle-verified: a case-mismatched header 400s even
    # though the body is well-formed; a case-correct header WITH trailing
    # params (charset) still matches (a prefix check, not equality).
    (
        "content_type_case_mismatch_json",
        "POST",
        "/translate",
        {
            "headers": {"content-type": "Application/JSON"},
            "content": b'{"q":"Hello","source":"en","target":"ru"}',
        },
    ),
    (
        "content_type_case_correct_with_charset",
        "POST",
        "/translate",
        {
            "headers": {"content-type": "application/json; charset=utf-8"},
            "content": b'{"q":"Hello","source":"en","target":"ru"}',
        },
    ),
    (
        "content_type_case_mismatch_form",
        "POST",
        "/translate",
        {
            "headers": {"content-type": "Application/X-WWW-Form-Urlencoded"},
            "content": b"q=Hello&source=en&target=ru",
        },
    ),
    # --- Final-review IMPORTANT 4: `alternatives` strictness, oracle-probed
    # per the reviewer's explicit list -- leading space, negative, leading
    # "+" (accepted), and u32 overflow -- each via BOTH form and multipart,
    # since the two content types produce genuinely different error shapes.
    ("alternatives_leading_space_form", "POST", "/translate", {"form": {**B, "alternatives": " 3"}}),
    (
        "alternatives_leading_space_multipart",
        "POST",
        "/translate",
        {"files": {k: (None, v) for k, v in {**B, "alternatives": " 3"}.items()}},
    ),
    ("alternatives_negative_form", "POST", "/translate", {"form": {**B, "alternatives": "-5"}}),
    (
        "alternatives_negative_multipart",
        "POST",
        "/translate",
        {"files": {k: (None, v) for k, v in {**B, "alternatives": "-5"}.items()}},
    ),
    ("alternatives_leading_plus_form", "POST", "/translate", {"form": {**B, "alternatives": "+3"}}),
    (
        "alternatives_leading_plus_multipart",
        "POST",
        "/translate",
        {"files": {k: (None, v) for k, v in {**B, "alternatives": "+3"}.items()}},
    ),
    (
        "alternatives_overflow_form",
        "POST",
        "/translate",
        {"form": {**B, "alternatives": "4294967296"}},
    ),
    (
        "alternatives_overflow_multipart",
        "POST",
        "/translate",
        {"files": {k: (None, v) for k, v in {**B, "alternatives": "4294967296"}.items()}},
    ),
    (
        "alternatives_max_u32_form",
        "POST",
        "/translate",
        {"form": {**B, "alternatives": "4294967295"}},
    ),
    (
        "alternatives_present_empty_form",
        "POST",
        "/translate",
        {"form": {**B, "alternatives": ""}},
    ),
    (
        "alternatives_present_empty_multipart",
        "POST",
        "/translate",
        {"files": {k: (None, v) for k, v in {**B, "alternatives": ""}.items()}},
    ),
    # --- Final-review guard: /detect + form garbage `alternatives` (the
    # shared `_parse_translate_payload` path is exercised by BOTH handlers;
    # the ledger specifically wants a /detect-side case, not just /translate).
    (
        "detect_form_alternatives_garbage",
        "POST",
        "/detect",
        {"form": {"q": "hi", "alternatives": "garbage"}},
    ),
]


def _case_param(case: tuple[str, str, str, dict[str, Any]]) -> Any:
    name, method, path, kw = case
    return pytest.param(name, method, path, kw, id=name)


@pytest.mark.parametrize("name,method,path,kw", [_case_param(c) for c in CASES])
def test_validation_parity(pair, name, method, path, kw) -> None:
    pair.reset()
    rust, py = fire_both(pair, method, path, **kw)
    assert_parity(rust, py)


AUTH_FLAGS = ["--api-key", "sekret"]


@pytest.mark.parametrize(
    "case",
    [
        "translate_no_key",
        "translate_wrong_key",
        "translate_right_key",
        "detect_no_key",
        "settings_still_lies",
        "badkey_and_overlimit_combo",
        "missing_q_and_bad_key",
    ],
)
def test_api_key_parity(make_pair, case) -> None:
    pair = make_pair(AUTH_FLAGS)
    pair.reset()
    body = {"q": "Hello", "source": "en", "target": "ru"}
    if case == "translate_no_key":
        rust, py = fire_both(pair, "POST", "/translate", json_body=body)
    elif case == "translate_wrong_key":
        rust, py = fire_both(pair, "POST", "/translate", json_body={**body, "api_key": "nope"})
    elif case == "translate_right_key":
        rust, py = fire_both(pair, "POST", "/translate", json_body={**body, "api_key": "sekret"})
    elif case == "detect_no_key":
        rust, py = fire_both(pair, "POST", "/detect", json_body={"q": "Hello"})
    elif case == "badkey_and_overlimit_combo":
        # Combined-violation ordering: the api-key check must win over the
        # (later) char-limit check even though this q is ALSO over the byte
        # limit — oracle-verified: 403 Invalid API key, not a 400 limit error.
        over_q = "я" * 2600
        rust, py = fire_both(
            pair,
            "POST",
            "/translate",
            json_body={"q": over_q, "source": "ru", "target": "en", "api_key": "wrong"},
        )
    elif case == "missing_q_and_bad_key":
        # Combined-violation ordering, the other direction from
        # badkey_and_overlimit_combo above: the missing-required-param check
        # is FIRST in Rust's check_params (loop over required_params, before
        # the api-key branch), so a missing "q" wins over a wrong api_key —
        # 400 "missing q parameter", not 403 "Invalid API key".
        rust, py = fire_both(
            pair,
            "POST",
            "/translate",
            json_body={"source": "en", "target": "ru", "api_key": "nope"},
        )
    else:  # settings_still_lies: Rust hardcodes keyRequired/apiKeys false even with a key set
        rust, py = fire_both(pair, "GET", "/frontend/settings")
    assert_parity(rust, py)


# ---------------------------------------------------------------------------
# Invalid-UTF-8 request bodies (v0.1.3). Before this, `json.loads(<bytes>)`
# raised `UnicodeDecodeError` — NOT a `json.JSONDecodeError`, so it escaped
# `_parse_translate_payload` entirely and Litestar answered a bare 500 where
# Rust answers a 400. Found at the smartech-translate P2 cutover gate.
#
# serde_json validates UTF-8 LAZILY: only for the contents of a string token,
# and only once the closing quote has been consumed — so the column is the END
# of the offending string, not the corruption site, and a bad byte OUTSIDE a
# string is an ordinary syntax error instead. Every case below was captured
# from `.rust-ref/target/release/ltengine` before being written down; the test
# itself asserts rust == py live, so a stale transcription cannot go green.
#
# Bodies are built from `bytes([...])` for the backslash/newline/tab bytes so
# no quoting layer between here and the wire can reinterpret them.
# ---------------------------------------------------------------------------

_BS = bytes([0x5C])
_NL = bytes([0x0A])
_TAB = bytes([0x09])


def _cyrillic_body(corrupt_at: int) -> bytes:
    """The production repro: a ~4.6 KiB JSON body with a Cyrillic `q`, two of
    its bytes overwritten with `\xff\xfe`. Rust reports column 4584 — the end
    of `q`, ~4.5 KiB past the corruption at byte 100."""
    unit = "Съешь же ещё этих мягких французских булок, да выпей чаю. "
    q = ""
    while len(q.encode("utf-8")) < 4500:
        q += unit
    raw = bytearray(
        json.dumps(
            {"q": q, "source": "ru", "target": "en", "format": "text"}, ensure_ascii=False
        ).encode("utf-8")
    )
    raw[corrupt_at : corrupt_at + 2] = b"\xff\xfe"
    return bytes(raw)


UTF8_CASES: list[tuple[str, bytes]] = [
    # --- the reported repro, and the same shape in miniature
    ("repro_5kb_cyrillic_at_100", _cyrillic_body(100)),
    ("in_string", b'{"q":"a\xffb","source":"en","target":"ru"}'),
    ("in_string_only_byte", b'{"q":"\xff","source":"en","target":"ru"}'),
    ("in_string_lone_continuation", b'{"q":"ab\x80cd"}'),
    ("in_string_long_prefix", b'{"q":"' + b"z" * 200 + b"\xff\xfe" + b"y" * 10 + b'"}'),
    # the bad byte is in the KEY, not the value
    ("in_key", b'{"q\xff":"x"}'),
    # a string whose closing quote is itself escaped-quote-adjacent
    ("in_string_after_escaped_quote", b'{"q":"a' + _BS + b'"b\xffc"}'),
    # rejected by Rust, ACCEPTED by CPython's `surrogatepass` decode — the
    # reason this path validates UTF-8 itself instead of trusting `json.loads`
    ("cesu8_surrogate", b'{"q":"\xed\xa0\x80"}'),
    ("overlong_encoding", b'{"q":"\xc0\xaf"}'),
    # --- structural boundary: NOT a UTF-8 error in Rust, an ordinary syntax one
    ("structural_after_brace", b'{\xff"q":"x"}'),
    ("structural_between_fields", b'{"q":"x",\xff"source":"en"}'),
    ("structural_leading", b'\xff{"q":"x"}'),
    ("structural_expects_colon", b'{"q"\xff:"x"}'),
    ("structural_expects_value", b'{"q":\xff}'),
    ("structural_expects_comma", b'{"q":"x"\xff}'),
    ("structural_after_whitespace", b'{"q":"x" \xff}'),
    # --- truncated multi-byte at EOF
    ("eof_truncated_midchar", b'{"q":"ab\xd0'),
    ("eof_truncated_string_closed", b'{"q":"ab\xd0"'),
    ("eof_truncated_multiline", b"{" + _NL + b'"q":"ab\xd0'),
    # --- multi-line: line is 1-indexed, column counts bytes since the line began
    ("multiline_in_string", b"{" + _NL + b'  "q": "ab\xffcd",' + _NL + b'  "source": "en"' + _NL + b"}"),
    ("multiline_structural", b"{" + _NL + b'  "q": "x",' + _NL + b'  \xff"source": "en"' + _NL + b"}"),
    ("multiline_second_string", b"{" + _NL + b'"a":"bbbb",' + _NL + b'"q":"xy\xffz"' + _NL + b"}"),
    # --- ordering: whichever fault serde reaches FIRST wins
    ("syntax_error_before_bad_byte", b'{"q" "x\xffy"}'),
    ("bad_byte_before_syntax_error", b'{"q":"a\xffb" "x"}'),
    ("bad_byte_is_the_escape_char", b'{"q":"a' + _BS + b'\xffb"}'),
    ("control_char_before_bad_byte", b'{"q":"a' + _NL + b'b\xffc"}'),
    ("control_char_after_bad_byte", b'{"q":"a\xffb' + _TAB + b'c"}'),
    # --- valid multibyte BEFORE the fault: serde counts BYTES, CPython chars
    ("cyrillic_then_bad_byte", '{"q":"привет'.encode() + b'\xffx"}'),
    ("cyrillic_earlier_string_clean", '{"a":"привет","q":"да'.encode() + b'\xff"}'),
    ("cyrillic_syntax_error", '{"q":"привет","source" "en"}'.encode()),
    ("cyrillic_typed_mismatch", '{"q":"привет","source":123,"target":"ru"}'.encode()),
    ("cyrillic_unterminated", '{"q":"привет'.encode()),
    # --- BOMs: CPython's `detect_encoding`/`utf-8-sig` handling is not serde's
    ("utf8_bom", b'\xef\xbb\xbf{"q":"x","source":"en","target":"ru"}'),
    ("utf16_bom", b'\xff\xfe{\x00"\x00q\x00"\x00'),
    # --- valid-UTF-8 anchors: these must not move
    ("valid_unterminated_string", b'{"q":"ab'),
    ("valid_unterminated_multiline", b"{" + _NL + b'"q":"ab'),
    ("valid_control_char", b'{"q":"a' + _NL + b'b"}'),
    ("valid_trailing_characters", b'{"a":1} x'),
]


@pytest.mark.parametrize("path", ["/translate", "/detect"])
@pytest.mark.parametrize("name,body", UTF8_CASES, ids=[c[0] for c in UTF8_CASES])
def test_invalid_utf8_body_parity(pair, name, body, path) -> None:
    """Both handlers share `_parse_translate_payload`, so both must answer
    identically — `/detect` is not a copy of the check, it is the same call."""
    pair.reset()
    rust, py = fire_both(
        pair, "POST", path, content=body, headers={"content-type": "application/json"}
    )
    assert rust.status == 400, f"unexpected rust status {rust.status} — re-check case"
    assert_parity(rust, py)
