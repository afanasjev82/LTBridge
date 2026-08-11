import pytest

from .helpers import assert_parity, fire_both
from .mock_upstream import MockBehavior

BODY = {"q": "Hello", "source": "en", "target": "ru"}

# (name, behavior). Expected statuses/bodies are NOT hardcoded — parity with
# Rust is the assertion. The dump script (Task 6 Step 2) documents observed values
# in .superpowers/sdd/task-6-report.md for humans; the tests only require rust == py.
ERROR_CASES = [
    ("upstream_502", MockBehavior(status=502)),
    ("upstream_503", MockBehavior(status=503)),
    ("upstream_504", MockBehavior(status=504)),
    ("upstream_429", MockBehavior(status=429)),
    (
        "upstream_400_with_body",
        MockBehavior(status=400, body_mode="custom", custom_body='{"detail":"boom"}'),
    ),
    ("empty_choices", MockBehavior(body_mode="empty_choices")),
]


@pytest.mark.parametrize("name,behavior", ERROR_CASES, ids=[c[0] for c in ERROR_CASES])
def test_llm_failure_parity(pair, name, behavior) -> None:
    pair.reset()
    pair.rust.mock.set(behavior)
    pair.py.mock.set(behavior)
    rust, py = fire_both(pair, "POST", "/translate", json_body=BODY)
    assert rust.status in (500, 503), f"unexpected rust status {rust.status} — re-check case"
    assert_parity(rust, py)


# Structural mismatches against ChatCompletionResponse's strict shape (valid
# upstream JSON, wrong shape) — CRITICAL fix-wave-2 finding. Confirmed
# empirically against the oracle that these hit the exact same Parse code path
# (and therefore the exact same generic, non-detailed message) as a
# JSON-syntax failure (garbage_json below), NOT a serde `missing field
# \`choices\`` style detail. See .superpowers/sdd/task-6-report.md, Fix wave 2.
STRUCTURAL_MISMATCH_CASES = [
    ("missing_choices", "{}"),
    ("choice_no_message", '{"choices":[{}]}'),
    ("message_no_content", '{"choices":[{"message":{}}]}'),
    ("content_null", '{"choices":[{"message":{"content":null}}]}'),
    ("body_null", "null"),
    ("body_string", '"str"'),
    ("body_array", "[1,2]"),
]


@pytest.mark.parametrize(
    "name,custom_body", STRUCTURAL_MISMATCH_CASES, ids=[c[0] for c in STRUCTURAL_MISMATCH_CASES]
)
def test_completion_shape_mismatch_parity(pair, name, custom_body) -> None:
    # Same URL-embedding caveat as test_garbage_json_llm_failure_parity below —
    # two independent mock instances on two different ports.
    behavior = MockBehavior(status=200, body_mode="custom", custom_body=custom_body)
    pair.reset()
    pair.rust.mock.set(behavior)
    pair.py.mock.set(behavior)
    rust, py = fire_both(pair, "POST", "/translate", json_body=BODY)
    assert rust.status == 500, f"unexpected rust status {rust.status} — re-check case"
    assert_parity(rust, py, normalize_upstream_url=True)


def test_garbage_json_llm_failure_parity(pair) -> None:
    # Same taxonomy as test_llm_failure_parity (500, parse failure), but the
    # message embeds each server's OWN upstream URL (reqwest's "error decoding
    # response body for url (<url>)" template — see errors.py/llm_client.py).
    # rust.mock and py.mock are two independent mock instances on different
    # ports by harness design (tests/differential/conftest.py `_make`), so that
    # substring can never be byte-identical between the two sides even though
    # the surrounding message text is. normalize_upstream_url strips it before
    # comparing — the same pattern normalize_heartbeat already uses for
    # timing-jittery content. See task-6-report.md for the full discussion.
    behavior = MockBehavior(body_mode="garbage")
    pair.reset()
    pair.rust.mock.set(behavior)
    pair.py.mock.set(behavior)
    rust, py = fire_both(pair, "POST", "/translate", json_body=BODY)
    assert rust.status == 500, f"unexpected rust status {rust.status} — re-check case"
    assert_parity(rust, py, normalize_upstream_url=True)


def test_unknown_path_parity(pair) -> None:
    rust, py = fire_both(pair, "GET", "/definitely-not-a-real-path-xyz")
    assert_parity(rust, py)


def test_method_not_allowed_parity(pair) -> None:
    # POST on a GET-only route falls through to Rust's embedded static-file
    # service, whose method guard (GET/HEAD only) rejects before any file
    # lookup — a generic 405, unaware of what /languages itself would accept.
    rust, py = fire_both(pair, "POST", "/languages")
    assert_parity(rust, py)


def test_malformed_json_parity(pair) -> None:
    rust, py = fire_both(
        pair,
        "POST",
        "/translate",
        headers={"content-type": "application/json"},
        content=b"{not json",
    )
    assert_parity(rust, py)


def test_malformed_json_empty_body_parity(pair) -> None:
    # IMPORTANT fix-wave-2 finding: serde_json's EOF column is bytes CONSUMED
    # (0 for an empty body), not CPython's 1-indexed colno — the previously
    # off-by-one EOF branch would have reported column 1 here. Oracle-verified:
    # HTTP 400, "Json deserialize error: EOF while parsing a value at line 1
    # column 0". See task-6-report.md, Fix wave 2.
    rust, py = fire_both(
        pair,
        "POST",
        "/translate",
        headers={"content-type": "application/json"},
        content=b"",
    )
    assert_parity(rust, py)


def test_malformed_json_truncated_object_parity(pair) -> None:
    # Same EOF-column fix, a second oracle-verified case: HTTP 400, "Json
    # deserialize error: EOF while parsing a value at line 1 column 5".
    rust, py = fire_both(
        pair,
        "POST",
        "/translate",
        headers={"content-type": "application/json"},
        content=b'{"a":',
    )
    assert_parity(rust, py)
