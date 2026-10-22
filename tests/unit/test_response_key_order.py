"""Final-review guard: every dynamically-keyed response-builder output must
serialize its keys in the same order `sorted()` would produce.

Rust's `serde_json::json!({...})` macro builds a `serde_json::Value::Object`,
which serializes via serde_json's default `BTreeMap` — ALPHABETICAL, not
insertion order. Every place this codebase builds one of those bodies by hand
must insert keys in that same fixed alphabetical order regardless of which
optional keys end up present, or a body that happens to omit one key would
silently drift out of alphabetical order without any test catching it. This
file pins that invariant directly against the extracted builders
(`routes._translate_response_body` / `routes._detection_json`) rather than
only exercising it indirectly through a live server.
"""

from __future__ import annotations

from typing import Any

import pytest
from litestar.testing import TestClient
from ltbridge.config import Config
from ltbridge.core.languages import DetectionResult, Language
from ltbridge.server.app import create_app
from ltbridge.server.routes import _detection_json, _translate_response_body

_LANG = Language(code="ru", name="Russian", targets=["en"], internal_code="ru")
_DETECTED = DetectionResult(language=_LANG, confidence=87)


def _translate_bodies() -> list[tuple[str, dict[str, Any]]]:
    return [
        ("no_alternatives_no_detection", _translate_response_body("hi", "hi", alternatives=0, detected=None)),
        ("alternatives_no_detection", _translate_response_body("hi", "hi", alternatives=2, detected=None)),
        (
            "no_alternatives_with_detection",
            _translate_response_body("hi", "hi", alternatives=0, detected=_DETECTED),
        ),
        (
            "alternatives_with_detection",
            _translate_response_body("hi", "hi", alternatives=3, detected=_DETECTED),
        ),
        # v0.1.5 opt-in slow-failure marker (`--mark-fallback`)
        (
            "fallback_only",
            _translate_response_body(
                "hi", "hi", alternatives=0, detected=None, fallback="llm_error"
            ),
        ),
        (
            "fallback_with_detection",
            _translate_response_body(
                "hi", "hi", alternatives=0, detected=_DETECTED, fallback="llm_timeout"
            ),
        ),
        (
            "fallback_with_alternatives_and_detection",
            _translate_response_body(
                "hi", "hi", alternatives=2, detected=_DETECTED, fallback="llm_timeout"
            ),
        ),
    ]


@pytest.mark.parametrize(
    "name,body", _translate_bodies(), ids=[c[0] for c in _translate_bodies()]
)
def test_translate_response_body_keys_are_alphabetical(name: str, body: dict[str, Any]) -> None:
    assert list(body.keys()) == sorted(body.keys()), body


def test_translate_response_body_nested_detected_language_keys_are_alphabetical() -> None:
    body = _translate_response_body("hi", "hi", alternatives=0, detected=_DETECTED)
    nested = body["detectedLanguage"]
    assert list(nested.keys()) == sorted(nested.keys()), nested


def test_translate_response_body_has_exactly_the_expected_keys() -> None:
    # Guards the ORDER test above against a vacuous pass on a single-key body
    # (trivially "sorted") -- confirms every optional key actually shows up
    # when it should.
    assert set(_translate_response_body("hi", "hi", alternatives=0, detected=None).keys()) == {
        "translatedText"
    }
    assert set(_translate_response_body("hi", "hi", alternatives=1, detected=None).keys()) == {
        "alternatives",
        "translatedText",
    }
    assert set(
        _translate_response_body("hi", "hi", alternatives=0, detected=_DETECTED).keys()
    ) == {"detectedLanguage", "translatedText"}
    assert set(
        _translate_response_body("hi", "hi", alternatives=1, detected=_DETECTED).keys()
    ) == {"alternatives", "detectedLanguage", "translatedText"}
    assert set(
        _translate_response_body("hi", "hi", alternatives=0, detected=None, fallback=None).keys()
    ) == {"translatedText"}
    assert set(
        _translate_response_body(
            "hi", "hi", alternatives=1, detected=_DETECTED, fallback="llm_timeout"
        ).keys()
    ) == {"alternatives", "detectedLanguage", "fallback", "translatedText"}


def test_translate_response_body_fallback_sits_between_detected_language_and_text() -> None:
    """The opt-in marker (v0.1.5) keeps the BTreeMap order a serde body would have.

    MUTATION: insert ``fallback`` after ``translatedText`` -> observed red (4
    failed: this test and the three ``fallback_*`` ids of the alphabetical
    test above).
    """
    body = _translate_response_body(
        "hi", "hi", alternatives=1, detected=_DETECTED, fallback="llm_timeout"
    )
    assert list(body) == ["alternatives", "detectedLanguage", "fallback", "translatedText"]
    assert body["fallback"] == "llm_timeout"


def test_detection_json_keys_are_alphabetical() -> None:
    body = _detection_json(_DETECTED)
    assert list(body.keys()) == sorted(body.keys()), body
    assert set(body.keys()) == {"confidence", "language"}


# ---------------------------------------------------------------------------
# The remaining response-builder shapes (`frontend_settings`, `/languages`
# list items, `/detect`'s top-level item) are literal dicts inline in their
# handler closures, not extracted top-level functions — exercised here
# through a live TestClient instead of a pure unit call. `httpx`/Litestar's
# `.json()` uses the stdlib `json` module, which preserves source/insertion
# order, so `list(body.keys())` reflects exactly what was sent on the wire.
# ---------------------------------------------------------------------------


def _client() -> TestClient:
    return TestClient(app=create_app(Config(llm_url="http://127.0.0.1:1", llm_model="m")))


def test_frontend_settings_top_level_keys_are_alphabetical() -> None:
    with _client() as client:
        body = client.get("/frontend/settings").json()
    assert list(body.keys()) == sorted(body.keys()), body


def test_frontend_settings_nested_language_keys_are_alphabetical() -> None:
    with _client() as client:
        body = client.get("/frontend/settings").json()
    nested = body["language"]
    assert list(nested.keys()) == sorted(nested.keys()), nested
    for side in ("source", "target"):
        assert list(nested[side].keys()) == sorted(nested[side].keys()), nested[side]


def test_languages_list_item_keys_are_alphabetical() -> None:
    with _client() as client:
        body = client.get("/languages").json()
    assert body, "expected at least one language"
    for item in body[:5]:
        assert list(item.keys()) == sorted(item.keys()), item


def test_detect_response_item_keys_are_alphabetical() -> None:
    with _client() as client:
        body = client.post("/detect", json={"q": "Hello world"}).json()
    assert list(body[0].keys()) == sorted(body[0].keys()), body[0]
