"""Pure unit coverage for errors.py's message-construction helpers."""

from __future__ import annotations

import json

import pytest
from ltbridge.core.errors import (
    bad_response,
    byte_position,
    empty_choices,
    invalid_utf8_error,
    json_deserialize_error,
    map_llm_failure,
    remote_status,
    timeout,
    unreachable,
)


def test_timeout_has_fixed_message_no_interpolation() -> None:
    # Timeout is a unit variant in Rust's ApiError; the observed text carries no
    # detail — the original task-brief skeleton's f"LLM API timed out: {detail}"
    # does not match the oracle.
    err = timeout()
    assert err.http_status == 503
    assert err.message == "LLM API request timed out"


def test_unreachable_embeds_detail() -> None:
    err = unreachable("boom")
    assert err.http_status == 503
    assert err.message == "LLM API unreachable: boom"


def test_remote_status_transient_maps_to_503() -> None:
    for status in (429, 502, 503, 504):
        err = remote_status(status, "body")
        assert err.http_status == 503
        assert err.message == f"LLM API returned status {status}: body"


def test_remote_status_other_maps_to_500() -> None:
    for status in (400, 401, 500):
        err = remote_status(status, "body")
        assert err.http_status == 500
        assert err.message == f"LLM API returned status {status}: body"


def test_bad_response_message_shape() -> None:
    err = bad_response("detail here")
    assert err.http_status == 500
    assert err.message == "LLM API response parse error: detail here"


def test_empty_choices_fixed_message() -> None:
    err = empty_choices()
    assert err.http_status == 500
    assert err.message == "LLM API returned no choices"


def test_map_llm_failure_tuple_delegates_to_remote_status() -> None:
    err = map_llm_failure((502, "upstream body"))
    assert err.http_status == 503
    assert err.message == "LLM API returned status 502: upstream body"


def test_json_deserialize_error_key_must_be_a_string() -> None:
    # The one case verified byte-for-byte against the Rust oracle — see
    # .superpowers/sdd/task-6-report.md.
    raw = b"{not json"
    try:
        json.loads(raw)
    except json.JSONDecodeError as exc:
        message = json_deserialize_error(raw, exc)
    else:  # pragma: no cover - sanity guard
        raise AssertionError("expected a JSONDecodeError")
    assert message == "Json deserialize error: key must be a string at line 1 column 2"


def test_json_deserialize_error_empty_body_eof_column() -> None:
    # IMPORTANT fix-wave-2 finding: serde_json's EOF column is bytes consumed
    # (0 for an empty body), not CPython's pos+1. Oracle-verified — see
    # .superpowers/sdd/task-6-report.md, Fix wave 2.
    raw = b""
    try:
        json.loads(raw)
    except json.JSONDecodeError as exc:
        message = json_deserialize_error(raw, exc)
    else:  # pragma: no cover - sanity guard
        raise AssertionError("expected a JSONDecodeError")
    assert message == "Json deserialize error: EOF while parsing a value at line 1 column 0"


def test_json_deserialize_error_truncated_object_eof_column() -> None:
    # Second oracle-verified EOF case: 5 bytes consumed -> column 5 (was 6
    # before the fix).
    raw = b'{"a":'
    try:
        json.loads(raw)
    except json.JSONDecodeError as exc:
        message = json_deserialize_error(raw, exc)
    else:  # pragma: no cover - sanity guard
        raise AssertionError("expected a JSONDecodeError")
    assert message == "Json deserialize error: EOF while parsing a value at line 1 column 5"


def test_json_deserialize_error_expected_value() -> None:
    # Source-derived (serde_json ErrorCode::ExpectedSomeValue -> "expected
    # value"), NOT independently verified against the live Rust binary.
    raw = b'{"q": }'
    try:
        json.loads(raw)
    except json.JSONDecodeError as exc:
        message = json_deserialize_error(raw, exc)
    else:  # pragma: no cover - sanity guard
        raise AssertionError("expected a JSONDecodeError")
    assert message.startswith("Json deserialize error: expected value at line 1 column")


# ---------------------------------------------------------------------------
# Invalid-UTF-8 bodies (v0.1.3). The differential suite (tests/differential/
# test_validation_parity.py::test_invalid_utf8_body_parity) is the authority —
# it fires each of these at the live Rust binary. What is pinned HERE is the
# message arithmetic itself, so a regression names the rule it broke instead of
# only showing up as one of forty parity diffs.
#
# Every expected string below was captured from
# `.rust-ref/target/release/ltengine` before being written down.
# ---------------------------------------------------------------------------

_BS = bytes([0x5C])
_NL = bytes([0x0A])


def test_byte_position_counts_bytes_not_characters() -> None:
    # Two-byte Cyrillic characters: byte offset 12 is character 6.
    raw = "привет".encode()
    assert byte_position(raw, 12) == (1, 12)
    assert byte_position(b"{" + _NL + b'"q"', 4) == (2, 2)


@pytest.mark.parametrize(
    "raw,expected",
    [
        # Inside a CLOSED string: serde validates only after consuming the
        # whole token, so the column is one past the CLOSING quote — NOT the
        # corruption site. `{"q":"a\xffb",...}` closes at byte 9.
        (b'{"q":"a\xffb","source":"en"}', "invalid unicode code point at line 1 column 10"),
        # 200 bytes of ASCII ahead of the bad byte: still the end of the string.
        (
            b'{"q":"' + b"z" * 200 + b"\xff\xfe" + b"y" * 10 + b'"}',
            "invalid unicode code point at line 1 column 219",
        ),
        # The bad byte in the KEY works the same way.
        (b'{"q\xff":"x"}', "invalid unicode code point at line 1 column 5"),
        # Valid multi-byte characters BEFORE the fault advance the column by
        # their ENCODED length — the whole reason byte_position exists.
        ('{"q":"привет'.encode() + b'\xffx"}', "invalid unicode code point at line 1 column 21"),
        # CESU-8 surrogate bytes: CPython's `surrogatepass` decode accepts
        # these, Rust does not — which is why this path validates UTF-8 itself.
        (b'{"q":"\xed\xa0\x80"}', "invalid unicode code point at line 1 column 10"),
        # OUTSIDE a string there is no UTF-8 check at all: serde just meets a
        # byte that starts no token, and reports an ordinary syntax error.
        (b'{\xff"q":"x"}', "key must be a string at line 1 column 2"),
        (b'\xff{"q":"x"}', "expected value at line 1 column 1"),
        (b'{"q":\xff}', "expected value at line 1 column 6"),
        # An unterminated string is reached FIRST: serde runs to end-of-input
        # looking for the closing quote, so this is an EOF position (column ==
        # bytes consumed), not the bad byte's.
        (b'{"q":"ab\xd0', "EOF while parsing a string at line 1 column 9"),
        # ... but close the string and the UTF-8 check fires after all.
        (b'{"q":"ab\xd0"', "invalid unicode code point at line 1 column 10"),
        # A fault serde reaches EARLIER wins, including one INSIDE the same
        # string token (a raw control character) — the comparison is against
        # the closing quote, not the opening one.
        (b'{"q" "x\xffy"}', "expected `:` at line 1 column 6"),
        (b'{"q":"a' + _NL + b'b\xffc"}',
         r"control character (\u0000-\u001F) found while parsing a string at line 2 column 0"),
        # The bad byte IS the escaped character -> "invalid escape", and serde
        # has consumed the backslash AND the escape char by then.
        (b'{"q":"a' + _BS + b'\xffb"}', "invalid escape at line 1 column 9"),
        # Line is 1-indexed; column counts bytes since that line began.
        (
            b"{" + _NL + b'"a":"bbbb",' + _NL + b'"q":"xy\xffz"' + _NL + b"}",
            "invalid unicode code point at line 3 column 10",
        ),
        # A UTF-16 BOM: CPython's `json.loads(bytes)` would decode the whole
        # body as UTF-16 here. serde has no BOM detection.
        (b'\xff\xfe{\x00"\x00q\x00"\x00', "expected value at line 1 column 1"),
    ],
)
def test_invalid_utf8_error_messages(raw: bytes, expected: str) -> None:
    assert invalid_utf8_error(raw) == f"Json deserialize error: {expected}"


def test_invalid_utf8_error_reports_end_of_q_on_a_large_cyrillic_body() -> None:
    """The reported cutover bug, at its original size: ~4.6 KiB of Cyrillic
    `q` corrupted at byte 100 reports column 4584 — the byte after `q`'s
    closing quote, ~4.5 KiB downstream of the corruption."""
    unit = "Съешь же ещё этих мягких французских булок, да выпей чаю. "
    q = ""
    while len(q.encode("utf-8")) < 4500:
        q += unit
    raw = bytearray(
        json.dumps(
            {"q": q, "source": "ru", "target": "en", "format": "text"}, ensure_ascii=False
        ).encode("utf-8")
    )
    raw[100:102] = b"\xff\xfe"
    assert invalid_utf8_error(bytes(raw)) == (
        "Json deserialize error: invalid unicode code point at line 1 column 4584"
    )


def test_json_deserialize_error_unterminated_string_runs_to_end_of_input() -> None:
    # Oracle-verified correction: CPython points `pos` at the OPENING quote
    # (column 6 here); serde scans to end-of-input, so the column is the bytes
    # consumed — 8. Pre-v0.1.3 this branch reported CPython's number.
    raw = b'{"q":"ab'
    with pytest.raises(json.JSONDecodeError) as excinfo:
        json.loads(raw)
    assert json_deserialize_error(raw, excinfo.value) == (
        "Json deserialize error: EOF while parsing a string at line 1 column 8"
    )


def test_json_deserialize_error_columns_are_bytes_on_a_multibyte_body() -> None:
    # The same char-vs-byte defect, on a body that IS valid UTF-8: CPython's
    # character count says column 24, serde counts bytes and says 30.
    raw = '{"q":"привет","source" "en"}'.encode()
    with pytest.raises(json.JSONDecodeError) as excinfo:
        json.loads(raw)
    assert json_deserialize_error(raw, excinfo.value) == (
        "Json deserialize error: expected `:` at line 1 column 30"
    )
