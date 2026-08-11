"""Pure unit coverage for errors.py's message-construction helpers."""

from __future__ import annotations

import json

from ltengine.errors import (
    bad_response,
    empty_choices,
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
