"""Structural guards for the pure-Python whatlang port.

The bulk of the parity evidence lives in ``test_detection_sweep.py`` (16k
recorded oracle outputs). This file pins the *premises* that sweep rests on —
the ones a future crate bump or CPython upgrade could quietly invalidate
without any recorded case changing.
"""

from __future__ import annotations

import pytest
from ltengine.core import _whatlang_data as D
from ltengine.core import whatlang
from ltengine.core.languages import _DETECT_ALLOWLIST, detect_language

# Rust's `sort_unstable` runs `small_sort_general` — which is stable — at or
# below this length, and a genuinely unstable quicksort above it. Measured
# against rustc 1.96: 0 divergences from a stable sort in 30k randomized
# tie-heavy trials per length up to 32, ~80% divergence at 33.
STABLE_SORT_BOUND = 32


def test_every_sorted_slice_stays_within_the_stable_sort_bound() -> None:
    """The port sorts with Python's stable ``sorted``; that is only exact <= 32."""
    allowed = {
        lang
        for lang, name in D.LANG_ENG_NAME.items()
        if whatlang._remap(name) in _DETECT_ALLOWLIST
    }
    assert len(D.SCRIPT_COUNTER_ORDER) <= STABLE_SORT_BOUND

    for script, langs in D.SCRIPT_LANGS.items():
        n_allowed = len([lang for lang in langs if lang in allowed])
        assert n_allowed <= STABLE_SORT_BOUND, f"{script}: {n_allowed} allowed languages"

    # The combined method unions the alphabet and trigram language lists.
    for script, profiles in D.TRIGRAM_PROFILES.items():
        union = {lang for lang, _ in profiles} | set(D.SCRIPT_LANGS[script])
        assert len(union & allowed) <= STABLE_SORT_BOUND, script


def test_generated_tables_have_the_expected_shape() -> None:
    assert D.CRATE == "whatlang"
    assert len(D.LANG_ORDER) == 70
    assert len(D.SCRIPT_COUNTER_ORDER) == 25
    assert len(set(D.LANG_ENG_NAME.values())) == 70, "eng_name collision breaks the allowlist"
    assert sum(len(p) for p in D.TRIGRAM_PROFILES.values()) == 51
    for profiles in D.TRIGRAM_PROFILES.values():
        for lang, blob in profiles:
            assert len(blob) == 3 * D.MAX_TRIGRAM_DISTANCE, lang


@pytest.mark.parametrize(
    ("code_point", "expected"),
    [
        # rustc 1.96 (Unicode 16.0) lowercases these; CPython 3.12 (15.0) does
        # not. Detection must follow rustc, so `_lowercase` may never delegate
        # to `str.lower()`.
        (0x1C89, "ᲊ"),
        (0xA7CB, "ɤ"),
        (0xA7CC, "ꟍ"),
        (0xA7CE, "꟏"),
        (0xA7D2, "ꟓ"),
        (0xA7D4, "ꟕ"),
        (0xA7DA, "ꟛ"),
        (0xA7DC, "ƛ"),
    ],
)
def test_lowercase_follows_the_pinned_rust_table(code_point: int, expected: str) -> None:
    assert whatlang._lowercase(chr(code_point)) == expected


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        # Captured from rustc 1.96's `str::to_lowercase`.
        ("aΣ", "aς"),
        ("αΣ", "ας"),
        ("Σ", "σ"),
        ("ΑΣ", "ας"),
        ("aΣb", "aσb"),
        ("aΣ ", "aς "),
        ("a Σ", "a σ"),
        ("1Σ", "1σ"),
        ("ΣΣ", "σς"),
        ("abcΣ", "abcς"),
        ("aΣαΣ", "aσας"),
        ("aʼΣ", "aʼς"),  # U+02BC is Case_Ignorable: skipped, then 'a' is Cased
        ("аΣ", "аς"),
        ("ΣaΣ", "σaς"),
    ],
)
def test_lowercase_implements_the_final_sigma_rule(text: str, expected: str) -> None:
    assert whatlang._lowercase(text) == expected


def test_latin_and_cyrillic_range_tables_overlap() -> None:
    """U+1D2B and U+1D78 belong to two scripts, so the counter scan order matters."""
    for cp in (0x1D2B, 0x1D78):
        scripts = {D.SCRIPT_COUNTER_ORDER[k] for k in whatlang._scripts_of(chr(cp))}
        assert scripts == {"Latin", "Cyrillic"}


def test_detect_without_an_allowlist_sees_unsupported_languages() -> None:
    """``allowlist=None`` mirrors ``FilterList::All``.

    The recorded sweep can only ever cover the allowlisted path (the binding
    hard-coded LTEngine's 49 names), so these expectations come straight from
    ``whatlang::detect`` under rustc 1.96 with default options — Croatian,
    Esperanto and German are not all reachable through our allowlist.
    """
    croatian = "Brzi smeđi lisac skače preko lijenog psa."
    assert whatlang.detect(croatian, None) == ("Croatian", 75)
    assert whatlang.detect(croatian, _DETECT_ALLOWLIST) == ("Slovak", 13)

    assert whatlang.detect("There is no reason not to learn Esperanto.", None) == ("English", 47)
    assert whatlang.detect("Mit dem Wissen wächst der Zweifel", None) == ("German", 100)


def test_undetectable_script_returns_none() -> None:
    for text in ("", " ", "123", "!!!", "😀"):
        assert whatlang.detect(text, _DETECT_ALLOWLIST) is None


def test_detect_language_keeps_the_libretranslate_shape() -> None:
    result = detect_language("Привет, как дела?")
    assert result.language.code == "ru"
    assert result.confidence == 23

    # Unknown / undetectable input falls back to English with confidence 0.
    fallback = detect_language("123")
    assert fallback.language.code == "en"
    assert fallback.confidence == 0

    # Amharic is detectable by whatlang but is not one of our 49 languages.
    assert detect_language("የኢትዮጵያ ፌዴራላዊ ዴሞክራሲያዊሪፐብሊክ").language.code == "en"
