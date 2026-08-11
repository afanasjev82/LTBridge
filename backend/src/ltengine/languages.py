"""Language registry and offline language detection.

Port of LTEngine's languages.rs using lingua-language-detector.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Optional

from lingua import Language as LinguaLanguage, LanguageDetectorBuilder


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

    code: str           # Public code returned in API responses (alias if present)
    name: str           # Display name
    targets: list[str]  # All other public codes (can translate to any language)
    internal_code: str  # Internal code used for lookups


def _build_languages() -> list[Language]:
    langs: list[Language] = []
    for internal_code, alias, name in _LANG_DEFS:
        public_code = alias if alias else internal_code
        langs.append(Language(
            code=public_code,
            name=name,
            targets=_ALL_PUBLIC_CODES[:],
            internal_code=internal_code,
        ))
    return langs


LANGUAGES: list[Language] = _build_languages()

# Lookup maps
_BY_INTERNAL: dict[str, Language] = {lang.internal_code: lang for lang in LANGUAGES}
_ALIAS_TO_INTERNAL: dict[str, str] = {}
for _code, _alias, _ in _LANG_DEFS:
    if _alias:
        _ALIAS_TO_INTERNAL[_alias] = _code
    _ALIAS_TO_INTERNAL[_code] = _code  # internal_code maps to itself


def get_language_by_code(code: str) -> Optional[Language]:
    """Resolve a public or internal language code to a Language object."""
    internal = _ALIAS_TO_INTERNAL.get(code)
    if internal is None:
        return None
    return _BY_INTERNAL.get(internal)


# ---------------------------------------------------------------------------
# Offline language detection via lingua-language-detector
# ---------------------------------------------------------------------------

# Map lingua Language enum values to our internal codes
_LINGUA_TO_INTERNAL: dict[str, str] = {
    "ENGLISH": "en",
    "ALBANIAN": "sq",
    "ARABIC": "ar",
    "AZERBAIJANI": "az",
    "BASQUE": "eu",
    "BENGALI": "bn",
    "BULGARIAN": "bg",
    "CATALAN": "ca",
    "CHINESE": "zh",
    "CZECH": "cs",
    "DANISH": "da",
    "DUTCH": "nl",
    "ESPERANTO": "eo",
    "ESTONIAN": "et",
    "FINNISH": "fi",
    "FRENCH": "fr",
    "GALICIAN": "gl",
    "GERMAN": "de",
    "GREEK": "el",
    "HEBREW": "he",
    "HINDI": "hi",
    "HUNGARIAN": "hu",
    "INDONESIAN": "id",
    "IRISH": "ga",
    "ITALIAN": "it",
    "JAPANESE": "ja",
    "KOREAN": "ko",
    "LATVIAN": "lv",
    "LITHUANIAN": "lt",
    "MALAY": "ms",
    "NORWEGIAN_BOKMAL": "nb",
    "PERSIAN": "fa",
    "POLISH": "pl",
    "PORTUGUESE": "pt",
    "ROMANIAN": "ro",
    "RUSSIAN": "ru",
    "SERBIAN": "sr",
    "SLOVAK": "sk",
    "SLOVENE": "sl",
    "SPANISH": "es",
    "SWEDISH": "sv",
    "TAGALOG": "tl",
    "THAI": "th",
    "TURKISH": "tr",
    "UKRAINIAN": "uk",
    "URDU": "ur",
    "VIETNAMESE": "vi",
}

# Build detector restricted to languages we actually support
_SUPPORTED_LINGUA = [
    lang for lang in LinguaLanguage.all()
    if lang.name in _LINGUA_TO_INTERNAL
]

_detector = (
    LanguageDetectorBuilder
    .from_languages(*_SUPPORTED_LINGUA)
    .with_preloaded_language_models()
    .build()
)


@dataclass
class DetectionResult:
    language: Language
    confidence: int  # 0-100


def detect_language(text: str) -> DetectionResult:
    """Detect the language of *text*. Returns English with confidence 0 on failure."""
    fallback = DetectionResult(language=LANGUAGES[0], confidence=0)
    result = _detector.detect_language_of(text)
    if result is None:
        return fallback

    internal_code = _LINGUA_TO_INTERNAL.get(result.name)
    if internal_code is None:
        return fallback

    lang = _BY_INTERNAL.get(internal_code)
    if lang is None:
        return fallback

    confidence_value = _detector.compute_language_confidence(text, result)
    confidence_pct = int(confidence_value * 100)
    return DetectionResult(language=lang, confidence=confidence_pct)
