"""Language registry and offline language detection.

Port of LTEngine's languages.rs. Detection runs on the pure-Python whatlang
port in :mod:`ltengine.core.whatlang` (tables generated from the whatlang
0.18.0 crate by ``scripts/gen_whatlang_data.py``), which reproduces the Rust
oracle's output exactly — see ``scripts/sweep_detection_parity.py``.
"""

from __future__ import annotations

from dataclasses import dataclass

from . import whatlang

# ---------------------------------------------------------------------------
# Language definitions (port of LANGS in languages.rs)
# Each entry: (internal_code, alias_or_empty, display_name)
# ---------------------------------------------------------------------------
_LANG_DEFS: list[tuple[str, str, str]] = [
    ("en", "", "English"),
    ("sq", "", "Albanian"),
    ("ar", "", "Arabic"),
    ("az", "", "Azerbaijani"),
    ("eu", "", "Basque"),
    ("bn", "", "Bengali"),
    ("bg", "", "Bulgarian"),
    ("ca", "", "Catalan"),
    ("zh", "zh-Hans", "Chinese"),
    ("zt", "zh-Hant", "Chinese (traditional)"),
    ("cs", "", "Czech"),
    ("da", "", "Danish"),
    ("nl", "", "Dutch"),
    ("eo", "", "Esperanto"),
    ("et", "", "Estonian"),
    ("fi", "", "Finnish"),
    ("fr", "", "French"),
    ("gl", "", "Galician"),
    ("de", "", "German"),
    ("el", "", "Greek"),
    ("he", "", "Hebrew"),
    ("hi", "", "Hindi"),
    ("hu", "", "Hungarian"),
    ("id", "", "Indonesian"),
    ("ga", "", "Irish"),
    ("it", "", "Italian"),
    ("ja", "", "Japanese"),
    ("ko", "", "Korean"),
    ("lv", "", "Latvian"),
    ("lt", "", "Lithuanian"),
    ("ms", "", "Malay"),
    ("nb", "", "Norwegian"),
    ("fa", "", "Persian"),
    ("pl", "", "Polish"),
    ("pt", "", "Portuguese"),
    ("pb", "pt-BR", "Portuguese (Brazil)"),
    ("ro", "", "Romanian"),
    ("ru", "", "Russian"),
    ("sr", "", "Serbian"),
    ("sk", "", "Slovak"),
    ("sl", "", "Slovenian"),
    ("es", "", "Spanish"),
    ("sv", "", "Swedish"),
    ("tl", "", "Tagalog"),
    ("th", "", "Thai"),
    ("tr", "", "Turkish"),
    ("uk", "", "Ukrainian"),
    ("ur", "", "Urdu"),
    ("vi", "", "Vietnamese"),
]

# Build list of all public codes (alias takes priority over internal code)
_ALL_PUBLIC_CODES = [alias if alias else code for code, alias, _ in _LANG_DEFS]


@dataclass
class Language:
    """A supported translation language."""

    code: str  # Public code returned in API responses (alias if present)
    name: str  # Display name
    targets: list[str]  # All other public codes (can translate to any language)
    internal_code: str  # Internal code used for lookups


def _build_languages() -> list[Language]:
    langs: list[Language] = []
    for internal_code, alias, name in _LANG_DEFS:
        public_code = alias if alias else internal_code
        langs.append(
            Language(
                code=public_code,
                name=name,
                targets=_ALL_PUBLIC_CODES[:],
                internal_code=internal_code,
            )
        )
    return langs


LANGUAGES: list[Language] = _build_languages()

# Lookup maps
_BY_INTERNAL: dict[str, Language] = {lang.internal_code: lang for lang in LANGUAGES}
_ALIAS_TO_INTERNAL: dict[str, str] = {}
for _code, _alias, _ in _LANG_DEFS:
    if _alias:
        _ALIAS_TO_INTERNAL[_alias] = _code
    _ALIAS_TO_INTERNAL[_code] = _code  # internal_code maps to itself


def get_language_by_code(code: str) -> Language | None:
    """Resolve a public or internal language code to a Language object."""
    internal = _ALIAS_TO_INTERNAL.get(code)
    if internal is None:
        return None
    return _BY_INTERNAL.get(internal)


# ---------------------------------------------------------------------------
# Offline language detection via the pure-Python whatlang port, parity with
# languages.rs's `detect_lang`.
# ---------------------------------------------------------------------------

# whatlang.detect() returns the display name after languages.rs's
# Mandarin->Chinese remap, so this is a direct name -> internal-code lookup
# over our own LANGS display names.
_NAME_TO_INTERNAL: dict[str, str] = {name: code for code, _alias, name in _LANG_DEFS}

# languages.rs builds its detector allowlist from these same display names:
# every whatlang language whose (remapped) English name is one of ours is
# detectable, the rest are not. Four of our names — "Chinese (traditional)",
# "Portuguese (Brazil)", "Norwegian" (whatlang: "Bokmal") and "Slovenian"
# (whatlang: "Slovene") — match nothing, exactly as in the Rust oracle.
_DETECT_ALLOWLIST: frozenset[str] = frozenset(_NAME_TO_INTERNAL)


@dataclass
class DetectionResult:
    language: Language
    confidence: int  # 0-100


def detect_language(text: str) -> DetectionResult:
    """Detect the language of *text* via whatlang. Returns English with confidence 0 on failure."""
    fallback = DetectionResult(language=LANGUAGES[0], confidence=0)
    result = whatlang.detect(text, _DETECT_ALLOWLIST)
    if result is None:
        return fallback

    name, confidence = result
    internal_code = _NAME_TO_INTERNAL.get(name)
    if internal_code is None:
        return fallback

    lang = _BY_INTERNAL.get(internal_code)
    if lang is None:
        return fallback

    return DetectionResult(language=lang, confidence=confidence)
