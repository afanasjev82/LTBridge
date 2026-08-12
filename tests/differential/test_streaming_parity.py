"""Grace-race / heartbeat-streaming parity.

Port target: main.rs's ``translate`` handler (``--features api``), which races
``run_prompt`` against ``GRACE_PERIOD`` (1 s) and, only if the LLM is still
pending, commits to a chunked 200 that emits one ``b" "`` per
``HEARTBEAT_INTERVAL`` (1 s) until the future resolves. Because the 200 is
already committed by then, a *slow* failure cannot be reported as a 5xx — Rust
falls back to ``res.unwrap_or_else(|_| q.clone())``, i.e. the ORIGINAL text,
still under HTTP 200. That is a bug-for-bug wart we must reproduce exactly; a
*fast* failure keeps the clean buffered error status (Task 6 taxonomy).

Timing note: Rust's heartbeat ticker is created after the grace period and its
first (immediate) tick is consumed, so the first space lands ~2 s into the
request. An upstream delay of exactly 2.0 s therefore *races* the first
heartbeat — verified against the oracle, which emitted 1 leading space on some
2.0 s runs and 0 on others. Every slow case below uses >= 2.5 s so that both
servers deterministically emit at least one heartbeat (``normalize_heartbeat``
requires >= 1 space on BOTH sides); the brief's nominal 2.0 s for the failure
case would be flaky on either implementation.
"""

from __future__ import annotations

import json
from typing import Any

import pytest

from .helpers import Snapshot, assert_parity, fire_both
from .mock_upstream import MockBehavior

B = {"q": "Hello", "source": "en", "target": "ru"}

SLOW_SUCCESS_DELAY_S = 3.0
SLOW_FAILURE_DELAY_S = 2.5


def _payload(snap: Snapshot) -> bytes:
    """The response body with the heartbeat prefix (and only that) stripped."""
    return snap.body.lstrip(b" ")


def test_slow_success_heartbeat(pair) -> None:
    pair.reset()
    for h in (pair.rust, pair.py):
        h.mock.set(MockBehavior(delay_s=SLOW_SUCCESS_DELAY_S))
    rust, py = fire_both(pair, "POST", "/translate", json_body=B, timeout=30.0)
    assert rust.status == 200 and rust.chunked and rust.leading_spaces >= 1
    assert_parity(rust, py, normalize_heartbeat=True)
    # assert_parity(normalize_heartbeat=True) compares PARSED json, which would
    # forgive an escaping/spacing difference; pin the bytes after the (jittery)
    # heartbeat prefix as well.
    assert _payload(py) == _payload(rust), f"payload: py={_payload(py)!r} rust={_payload(rust)!r}"


def test_slow_failure_returns_200_original_text(pair) -> None:
    """The bug-for-bug wart: >1s then failure => chunked 200 with original q."""
    pair.reset()
    for h in (pair.rust, pair.py):
        h.mock.set(MockBehavior(delay_s=SLOW_FAILURE_DELAY_S, status=502))
    rust, py = fire_both(pair, "POST", "/translate", json_body=B, timeout=30.0)
    assert rust.status == 200
    assert rust.json == {"translatedText": "Hello"}
    assert_parity(rust, py, normalize_heartbeat=True)
    assert _payload(py) == _payload(rust)


def test_fast_path_stays_buffered(pair) -> None:
    pair.reset()
    rust, py = fire_both(pair, "POST", "/translate", json_body=B)
    assert not rust.chunked and rust.leading_spaces == 0
    assert not py.chunked and py.leading_spaces == 0
    assert_parity(rust, py)


def test_slow_resolution_inside_first_heartbeat(pair) -> None:
    """Grace elapses with the task still pending (delay 1.5s > 1.0s grace), so
    the 200 commits to chunked at the grace boundary — but the upstream then
    resolves before the FIRST heartbeat's own 1s window elapses (1.5s < 2.0s),
    so zero heartbeat spaces are ever emitted. Distinct from both the buffered
    fast path (never chunked) and the >=1-heartbeat slow path.
    """
    pair.reset()
    for h in (pair.rust, pair.py):
        h.mock.set(MockBehavior(delay_s=1.5))
    rust, py = fire_both(pair, "POST", "/translate", json_body=B, timeout=30.0)
    assert rust.chunked and py.chunked
    assert rust.leading_spaces == 0 and py.leading_spaces == 0
    assert_parity(rust, py)


def test_late_but_fast_failure_stays_buffered(pair) -> None:
    """A failure at 0.5s (well under the 1.0s grace) is still the FAST path:
    a buffered 503, not the chunked slow-failure wart — even though 0.5s is
    "late" relative to a typical fast response.
    """
    pair.reset()
    for h in (pair.rust, pair.py):
        h.mock.set(MockBehavior(delay_s=0.5, status=502))
    rust, py = fire_both(pair, "POST", "/translate", json_body=B, timeout=30.0)
    assert rust.status == 503 and not rust.chunked
    assert py.status == 503 and not py.chunked
    assert_parity(rust, py)


def test_slow_success_non_ascii_is_raw_utf8(pair) -> None:
    """Serialization trap: the streamed body must be byte-identical to the
    buffered one, i.e. msgspec/serde-style compact + RAW UTF-8.

    ``json.dumps`` defaults (``ensure_ascii=True``, ``", "``/``": "`` separators)
    would emit ``\\u041f\\u0440...`` with spaces and diverge from serde_json's
    ``\xd0\x9f\xd1\x80...`` on any non-ASCII translation, while still parsing
    equal — which is why this case asserts BYTES, not just ``assert_parity``.
    """
    pair.reset()
    for h in (pair.rust, pair.py):
        h.mock.set(MockBehavior(delay_s=SLOW_SUCCESS_DELAY_S, reply_text="Привет"))
    rust, py = fire_both(pair, "POST", "/translate", json_body=B, timeout=30.0)
    assert rust.status == 200 and rust.chunked
    assert _payload(rust) == '{"translatedText":"Привет"}'.encode()
    assert_parity(rust, py, normalize_heartbeat=True)
    assert _payload(py) == _payload(rust), f"payload: py={_payload(py)!r} rust={_payload(rust)!r}"


def _split_detected(snap: Snapshot) -> tuple[list[str], Any, dict[str, Any]]:
    """Return (top-level key order, detectedLanguage value, rest-of-body).

    ``detectedLanguage``'s VALUES cannot be compared across the two servers yet:
    Rust detects via whatlang, Python via lingua, and they disagree (oracle:
    ``{"confidence":3,"language":"da"}`` for "Hello"; Python: ``it``/17).
    Detection parity is Task 9's job — this helper isolates it so the Task 8
    property (does ``detectedLanguage`` still appear on the slow-FAILURE
    fallback body, and in the right alphabetical slot?) is testable now.
    """
    body = json.loads(_payload(snap), object_pairs_hook=list)
    keys = [k for k, _ in body]
    rest = {k: v for k, v in body if k != "detectedLanguage"}
    detected = dict(next((v for k, v in body if k == "detectedLanguage"), []))
    return keys, detected, rest


def test_slow_failure_auto_source_keeps_detected_language(pair) -> None:
    """Oracle-verified: the slow-failure fallback body is built by the SAME
    ``translate_response_json`` the success path uses, so ``detectedLanguage``
    (source="auto") and ``alternatives`` still appear, alphabetically ordered,
    around the ORIGINAL text::

        {"alternatives":[],"detectedLanguage":{"confidence":3,"language":"da"},
         "translatedText":"Hello"}
    """
    pair.reset()
    for h in (pair.rust, pair.py):
        h.mock.set(MockBehavior(delay_s=SLOW_FAILURE_DELAY_S, status=502))
    body = {"q": "Hello", "source": "auto", "target": "ru", "alternatives": 2}
    rust, py = fire_both(pair, "POST", "/translate", json_body=body, timeout=30.0)

    assert rust.status == 200 and rust.chunked and rust.leading_spaces >= 1
    assert py.status == rust.status
    assert py.chunked and py.leading_spaces >= 1

    rust_keys, rust_detected, rust_rest = _split_detected(rust)
    py_keys, py_detected, py_rest = _split_detected(py)

    assert rust_keys == ["alternatives", "detectedLanguage", "translatedText"]
    assert py_keys == rust_keys, f"key order: py={py_keys} rust={rust_keys}"
    assert rust_rest == {"alternatives": [], "translatedText": "Hello"}
    assert py_rest == rust_rest, f"body (minus detection): py={py_rest} rust={rust_rest}"
    if py_detected == rust_detected:
        pytest.fail("Task 9 landed — tighten this test to full assert_parity")
    # Values differ until Task 9 ports whatlang; the SHAPE must already match.
    assert list(py_detected) == list(rust_detected) == ["confidence", "language"]
    assert isinstance(py_detected["confidence"], int)
    assert isinstance(py_detected["language"], str)
