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
    # --- Combined-violation ordering: missing-param wins over the (separate,
    # later) format check — matches Rust's check_params-then-check_format
    # sequencing exactly.
    (
        "missing_q_and_bad_format",
        "POST",
        "/translate",
        {"json_body": {"source": "en", "target": "ru", "format": "pdf"}},
    ),
]


#: Detection-dependent cases: the manager's `detect_language` (lingua) doesn't
#: match Rust's whatlang yet (Task 9). Everything else in CASES must be green
#: in THIS task; these are the only sanctioned strict xfails.
XFAIL_DETECTION_DEPENDENT = {"detect_ok"}


def _case_param(case: tuple[str, str, str, dict[str, Any]]) -> Any:
    name, method, path, kw = case
    marks = (
        [pytest.mark.xfail(reason="whatlang parity lands in Task 9", strict=True)]
        if name in XFAIL_DETECTION_DEPENDENT
        else []
    )
    return pytest.param(name, method, path, kw, marks=marks, id=name)


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
    else:  # settings_still_lies: Rust hardcodes keyRequired/apiKeys false even with a key set
        rust, py = fire_both(pair, "GET", "/frontend/settings")
    assert_parity(rust, py)
