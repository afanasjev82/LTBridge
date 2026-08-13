"""Pure-Python port of the whatlang language detector (crate 0.18.0).

This replaces the vendored PyO3 binding (``whatlang_py``) that used to back
:mod:`ltengine.core.languages`. It reproduces, operation for operation, the call
path that binding exercised — ``Detector::with_allowlist(langs).detect(text)`` —
so that ``/detect`` and ``source="auto"`` stay byte-identical to the Rust
LTEngine oracle. All tables live in the generated :mod:`._whatlang_data`; this
module is only the algorithm.

The pipeline (whatlang ``src/core/detect.rs``)::

    raw_detect_script(text)          # 25 counters over the ORIGINAL text
      -> main_script (highest count, None if every counter is 0)
      -> ScriptLangGroup:
           One(lang)   -> (lang, confidence 1.0), allowlist NOT consulted
           Mandarin    -> Mandarin/Japanese split by kana ratio
           Multi(mls)  -> combined::detect  (alphabet score + trigram score)

Fidelity notes — the places where a naive port would drift:

* **Sorting.** whatlang uses ``sort_unstable*`` in four places. Rust's unstable
  sort is only *unstable* above its small-sort threshold: for slices of 32 or
  fewer elements it runs ``small_sort_general``, which is stable, so its output
  is bit-identical to a stable sort (measured against rustc 1.96: 0 divergences
  in 30k randomized tie-heavy trials per length for n <= 32, ~80% divergence at
  n = 33). Every slice sorted here is bounded by the number of *allowed*
  languages for one script — 25 for Latin under LTEngine's 49-name allowlist —
  and the script-counter slice is always 25. Python's stable ``sorted`` is
  therefore exact. ``tests/unit/test_whatlang_port.py`` pins the <= 32 bound.
* **Script scan.** ``raw_detect_script`` mutates its counter array as it goes
  (each hit is swapped one slot towards the front), and the Latin and Cyrillic
  range tables *overlap* on U+1D2B and U+1D78 — so which counter a character
  lands in genuinely depends on the current order. The scan and the swaps are
  reproduced exactly.
* **Integer arithmetic.** The trigram distance is computed in ``u32`` with
  release-mode wrapping; the two subtractions that could in principle wrap are
  masked to 32 bits rather than trusted to stay non-negative.
* **Lowercasing.** whatlang calls ``str::to_lowercase``, i.e. the *Rust*
  standard library's Unicode tables, which are snapshotted into
  ``_whatlang_data`` rather than delegated to ``str.lower()`` — CPython 3.12
  ships Unicode 15.0 against rustc 1.96's 16.0 and the two disagree on 55 code
  points, 8 of them inside whatlang's own Latin range. Pinning the table also
  means a future CPython Unicode bump cannot silently move detection.
"""

from __future__ import annotations

from bisect import bisect_right
from math import isnan

from . import _whatlang_data as _d

__all__ = ["detect"]

# ---------------------------------------------------------------------------
# Derived lookup tables (built once at import)
# ---------------------------------------------------------------------------

_STOP_CHARS = frozenset(
    chr(cp) for lo, hi in _d.STOP_CHAR_RANGES for cp in range(lo, hi + 1)
)

_SCRIPTS: tuple[str, ...] = _d.SCRIPT_COUNTER_ORDER
_N_SCRIPTS = len(_SCRIPTS)

# Per-script range tables, sorted for bisect. Kept per script (not merged)
# because the tables are NOT disjoint: U+1D2B and U+1D78 are both Latin and
# Cyrillic, and which one wins depends on the live counter order.
_SCRIPT_STARTS: tuple[tuple[int, ...], ...] = tuple(
    tuple(lo for lo, _ in sorted(_d.SCRIPT_CHAR_RANGES[s])) for s in _SCRIPTS
)
_SCRIPT_ENDS: tuple[tuple[int, ...], ...] = tuple(
    tuple(hi for _, hi in sorted(_d.SCRIPT_CHAR_RANGES[s])) for s in _SCRIPTS
)

_MAX_TRIGRAM_DISTANCE: int = _d.MAX_TRIGRAM_DISTANCE
_MAX_TOTAL_DISTANCE: int = _d.MAX_TOTAL_DISTANCE
_TEXT_TRIGRAMS_SIZE: int = _d.TEXT_TRIGRAMS_SIZE
_U32 = 0xFFFFFFFF

_SIGMA = "Σ"
_SIGMA_LOWER = "σ"
_SIGMA_FINAL = "ς"

_CASE_IGNORABLE_STARTS = tuple(lo for lo, _ in _d.CASE_IGNORABLE_RANGES)
_CASE_IGNORABLE_ENDS = tuple(hi for _, hi in _d.CASE_IGNORABLE_RANGES)
_CASED_STARTS = tuple(lo for lo, _ in _d.CASED_RANGES)
_CASED_ENDS = tuple(hi for _, hi in _d.CASED_RANGES)

_LOWER = _d.LOWERCASE_MAP


def _alphabet_tables(script: str) -> tuple[dict[str, tuple[str, ...]], int]:
    """Rust ``build_inverted_map``: character -> the languages whose alphabet has it."""
    inverted: dict[str, list[str]] = {}
    for lang, alphabet in _d.ALPHABETS[script]:
        for ch in alphabet:
            inverted.setdefault(ch, []).append(lang)
    return {ch: tuple(langs) for ch, langs in inverted.items()}, len(
        _d.SCRIPT_LANGS[script]
    )


# script -> (char -> langs, number of languages that script has)
_ALPHABET_MAPS: dict[str, tuple[dict[str, tuple[str, ...]], int]] = {
    script: _alphabet_tables(script) for script in _d.ALPHABETS
}

# script -> ((lang, {trigram: rank}, [trigram, ...]), ...) in profile order
_PROFILES: dict[str, tuple[tuple[str, dict[str, int], tuple[str, ...]], ...]] = {
    script: tuple(
        (
            lang,
            {blob[i * 3 : i * 3 + 3]: i for i in range(len(blob) // 3)},
            tuple(blob[i * 3 : i * 3 + 3] for i in range(len(blob) // 3)),
        )
        for lang, blob in profiles
    )
    for script, profiles in _d.TRIGRAM_PROFILES.items()
}

_MANDARIN_IDX = _SCRIPTS.index("Mandarin")
_KATAKANA_IDX = _SCRIPTS.index("Katakana")
_HIRAGANA_IDX = _SCRIPTS.index("Hiragana")


def _remap(eng_name: str) -> str:
    """LTEngine's ``eng_name_map``: whatlang's "Mandarin" is our "Chinese"."""
    return "Chinese" if eng_name == "Mandarin" else eng_name


_ENG_NAME: dict[str, str] = {
    lang: _remap(name) for lang, name in _d.LANG_ENG_NAME.items()
}


# ---------------------------------------------------------------------------
# Character predicates
# ---------------------------------------------------------------------------


def _in_ranges(cp: int, starts: tuple[int, ...], ends: tuple[int, ...]) -> bool:
    i = bisect_right(starts, cp) - 1
    return i >= 0 and cp <= ends[i]


_script_cache: dict[str, tuple[int, ...]] = {}


def _scripts_of(ch: str) -> tuple[int, ...]:
    """Indices (into ``_SCRIPTS``) of every script whose range table contains *ch*."""
    hit = _script_cache.get(ch)
    if hit is None:
        cp = ord(ch)
        hit = tuple(
            k
            for k in range(_N_SCRIPTS)
            if _in_ranges(cp, _SCRIPT_STARTS[k], _SCRIPT_ENDS[k])
        )
        _script_cache[ch] = hit
    return hit


# ---------------------------------------------------------------------------
# Lowercasing (Rust `str::to_lowercase`, including the Final_Sigma rule)
# ---------------------------------------------------------------------------


def _case_ignorable(ch: str) -> bool:
    return _in_ranges(ord(ch), _CASE_IGNORABLE_STARTS, _CASE_IGNORABLE_ENDS)


def _cased(ch: str) -> bool:
    return _in_ranges(ord(ch), _CASED_STARTS, _CASED_ENDS)


def _sigma_is_word_final(text: str, i: int) -> bool:
    """Unicode ``Final_Sigma``: preceded by a cased character, not followed by one.

    Rust spells this as ``case_ignorable_then_cased(before.rev()) &&
    !case_ignorable_then_cased(after)`` — skip Case_Ignorable characters, then
    test Cased on the first survivor (an exhausted run is false). Walking
    indices rather than materialising the two slices keeps a text with many
    sigmas linear instead of quadratic.
    """
    j = i - 1
    while j >= 0 and _case_ignorable(text[j]):
        j -= 1
    if not (j >= 0 and _cased(text[j])):
        return False

    j, n = i + 1, len(text)
    while j < n and _case_ignorable(text[j]):
        j += 1
    return not (j < n and _cased(text[j]))


def _lowercase(text: str) -> str:
    """Rust ``str::to_lowercase`` over the snapshotted std tables."""
    if text.isascii():
        # ASCII case mapping is identical in both runtimes and contains no sigma.
        return text.lower()
    if _SIGMA not in text:
        return "".join([_LOWER.get(ch, ch) for ch in text])

    out: list[str] = []
    for i, ch in enumerate(text):
        if ch == _SIGMA:
            out.append(_SIGMA_FINAL if _sigma_is_word_final(text, i) else _SIGMA_LOWER)
        else:
            out.append(_LOWER.get(ch, ch))
    return "".join(out)


# ---------------------------------------------------------------------------
# Script detection (whatlang src/scripts/detect.rs)
# ---------------------------------------------------------------------------


def _raw_detect_script(text: str) -> tuple[list[tuple[str, int]], list[int]]:
    """Return the counter list (sorted by count, descending) and raw counts by index."""
    order = list(range(_N_SCRIPTS))
    position = list(range(_N_SCRIPTS))
    counts = [0] * _N_SCRIPTS

    for ch in text:
        if ch in _STOP_CHARS:
            continue
        matches = _scripts_of(ch)
        if not matches:
            continue
        # The Rust scan walks the (mutating) counter array front to back and
        # stops at the first predicate that fires.
        i = position[matches[0]] if len(matches) == 1 else min(position[k] for k in matches)
        counts[order[i]] += 1
        if i > 0:
            a, b = order[i - 1], order[i]
            order[i - 1], order[i] = b, a
            position[b], position[a] = i - 1, i

    counters = [(_SCRIPTS[k], counts[k]) for k in order]
    counters.sort(key=lambda pair: pair[1], reverse=True)
    return counters, counts


# ---------------------------------------------------------------------------
# Alphabet method (whatlang src/alphabets/)
# ---------------------------------------------------------------------------


def _alphabet_raw_detect(
    lowercase: str, script: str, allowed: frozenset[str] | None
) -> tuple[int, list[tuple[str, float]]]:
    """Return ``(count, normalized_scores)`` mirroring ``alphabets::raw_detect``."""
    mock = _d.MOCK_ALPHABET_LANGS.get(script)
    if mock is not None:
        # build_mock(): count 1, every allowed language scores 1.0, no sorting.
        return 1, [(lang, 1.0) for lang in mock if allowed is None or lang in allowed]

    char_langs, n_script_langs = _ALPHABET_MAPS[script]

    char_scores: dict[str, int] = {}
    max_raw_score = 0
    for ch in lowercase:
        if ch in _STOP_CHARS:
            continue
        max_raw_score += 1
        if ch in char_langs:
            char_scores[ch] = char_scores.get(ch, 0) + 2

    lang_scores: dict[str, int] = {}
    common_score = 0
    for ch, char_score in char_scores.items():
        langs = char_langs[ch]
        if len(langs) == n_script_langs:
            common_score += char_score
        else:
            for lang in langs:
                lang_scores[lang] = lang_scores.get(lang, 0) + char_score

    raw_scores: list[tuple[str, int]] = []
    for lang in _d.SCRIPT_LANGS[script]:
        if allowed is not None and lang not in allowed:
            continue
        # usize saturating_sub
        raw_scores.append((lang, max(0, lang_scores.get(lang, 0) + common_score - max_raw_score)))
    raw_scores.sort(key=lambda pair: pair[1], reverse=True)

    if max_raw_score == 0:
        # Rust: `0 as f64 / 0 as f64` == NaN. Unreachable in practice (a script
        # was detected, so at least one non-stop character survives lowercasing).
        nan = float("nan")
        return 0, [(lang, nan) for lang, _ in raw_scores]
    return max_raw_score, [(lang, score / max_raw_score) for lang, score in raw_scores]


# ---------------------------------------------------------------------------
# Trigram method (whatlang src/trigrams/)
# ---------------------------------------------------------------------------


def _trigram_positions(lowercase: str) -> dict[str, int]:
    """whatlang ``get_trigrams_with_positions``: trigram -> rank by descending frequency."""
    occurrences: dict[str, int] = {}
    chars = [" " if ch in _STOP_CHARS else ch for ch in lowercase]
    chars.append(" ")

    c1 = " "
    c2 = chars[0]
    for c3 in chars[1:]:
        if not (c2 == " " and (c1 == " " or c3 == " ")):
            key = c1 + c2 + c3
            occurrences[key] = occurrences.get(key, 0) + 1
        c1, c2 = c2, c3

    # Rust sorts `(count, trigram)` pairs with `b.cmp(a)`: descending by count,
    # then descending by trigram (a plain code-point-wise comparison, which is
    # what Python's str comparison does for these equal-length keys). The keys
    # are unique, so the ordering is total and stability is irrelevant.
    ranked = sorted(
        ((count, trigram) for trigram, count in occurrences.items()), reverse=True
    )
    return {trigram: i for i, (_, trigram) in enumerate(ranked[:_TEXT_TRIGRAMS_SIZE])}


def _calculate_distance(
    profile_index: dict[str, int],
    profile_list: tuple[str, ...],
    text_positions: dict[str, int],
) -> int:
    count = len(text_positions)
    hits = 0
    total = 0
    # Sum over the profile's 300 entries; iterating whichever side is smaller
    # yields the same integer sum (only matched pairs contribute a |n - i|).
    if count < len(profile_list):
        for trigram, position in text_positions.items():
            maybe_rank = profile_index.get(trigram)
            if maybe_rank is not None:
                total += abs(position - maybe_rank)
                hits += 1
    else:
        for rank, entry in enumerate(profile_list):
            maybe_position = text_positions.get(entry)
            if maybe_position is not None:
                total += abs(maybe_position - rank)
                hits += 1
    total += (len(profile_list) - hits) * _MAX_TRIGRAM_DISTANCE

    if _MAX_TRIGRAM_DISTANCE > count:
        delta = _MAX_TRIGRAM_DISTANCE - count
        total = (total - delta * _MAX_TRIGRAM_DISTANCE) & _U32  # u32, release-mode wrap
    return min(max(total, 0), _MAX_TOTAL_DISTANCE)


def _trigram_raw_detect(
    lowercase: str, script: str, allowed: frozenset[str] | None
) -> tuple[int, list[tuple[str, float]]]:
    text_positions = _trigram_positions(lowercase)
    unique_trigrams_count = len(text_positions)

    distances: list[tuple[str, int]] = []
    for lang, profile_index, profile_list in _PROFILES[script]:
        if allowed is not None and lang not in allowed:
            continue
        distances.append((lang, _calculate_distance(profile_index, profile_list, text_positions)))
    distances.sort(key=lambda pair: pair[1])

    max_dist = unique_trigrams_count * _MAX_TRIGRAM_DISTANCE
    if max_dist == 0:
        nan = float("nan")
        return unique_trigrams_count, [(lang, nan) for lang, _ in distances]
    return unique_trigrams_count, [
        (lang, ((max_dist - dist) & _U32) / max_dist) for lang, dist in distances
    ]


# ---------------------------------------------------------------------------
# Combined method (whatlang src/combined/mod.rs)
# ---------------------------------------------------------------------------


def _calc_alphabet_weight(count: int) -> float:
    weight = -(count / 300.0) + 2.0 / 3.0
    return min(max(weight, 1.0 / 3.0), 2.0 / 3.0)


def _calculate_confidence(highest: float, second: float, count: int) -> float:
    """whatlang ``core::calculate_confidence``."""
    if highest == 0.0:
        return 0.0
    if second == 0.0:
        return highest
    confident_rate = (3.0 / count if count else float("inf")) + 0.015
    rate = (highest - second) / second
    return 1.0 if rate > confident_rate else rate / confident_rate


def _combined_detect(
    text: str, script: str, allowed: frozenset[str] | None
) -> tuple[str, float] | None:
    lowercase = _lowercase(text)
    alphabet_count, alphabet_scores = _alphabet_raw_detect(lowercase, script, allowed)
    trigrams_count, trigram_scores = _trigram_raw_detect(lowercase, script, allowed)

    alphabet_by_lang = dict(alphabet_scores)
    trigram_by_lang = dict(trigram_scores)

    all_langs = [lang for lang, _ in alphabet_scores]
    seen = set(all_langs)
    for lang, _ in trigram_scores:
        if lang not in seen:
            seen.add(lang)
            all_langs.append(lang)

    alphabet_weight = _calc_alphabet_weight(alphabet_count)
    trigram_weight = 1.0 - alphabet_weight

    scores = [
        (
            lang,
            alphabet_by_lang.get(lang, 0.0) * alphabet_weight
            + trigram_by_lang.get(lang, 0.0) * trigram_weight,
        )
        for lang in all_langs
    ]
    scores.sort(key=lambda pair: pair[1], reverse=True)

    if not scores:
        return None
    lang1, score1 = scores[0]
    if len(scores) < 2:
        return lang1, 1.0
    return lang1, _calculate_confidence(score1, scores[1][1], trigrams_count)


# ---------------------------------------------------------------------------
# Mandarin / Japanese split (whatlang detect_lang_base_on_mandarin_script)
# ---------------------------------------------------------------------------


def _detect_mandarin(counts: list[int], allowed: frozenset[str] | None) -> tuple[str, float]:
    if allowed is not None and "Cmn" not in allowed:
        return "Jpn", 1.0

    mandarin_count = counts[_MANDARIN_IDX]
    japanese_count = counts[_KATAKANA_IDX] + counts[_HIRAGANA_IDX]
    total = mandarin_count + japanese_count
    jpn_pct = japanese_count / total

    if jpn_pct > 0.2:
        return "Jpn", 1.0
    if jpn_pct > 0.05:
        return "Jpn", 0.5
    if jpn_pct > 0.02:
        return "Cmn", 0.5
    return "Cmn", 1.0


# ---------------------------------------------------------------------------
# Public entry point
# ---------------------------------------------------------------------------


def _as_i32(value: float) -> int:
    """Rust's ``as i32`` cast: truncate towards zero, saturating, NaN -> 0."""
    if isnan(value):
        return 0
    if value >= 2147483648.0:
        return 2147483647
    if value <= -2147483649.0:
        return -2147483648
    return int(value)


def detect(text: str, allowlist: frozenset[str] | None = None) -> tuple[str, int] | None:
    """Detect the language of *text*, restricted to *allowlist*.

    Args:
        text: The text to classify.
        allowlist: English display names (after the Mandarin -> Chinese remap)
            of the languages the detector may return, mirroring
            ``Detector::with_allowlist``. ``None`` allows every language.

    Returns:
        ``(english_display_name, confidence_0_to_100)``, or ``None`` when no
        script could be identified — the same shape the ``whatlang_py`` binding
        returned, so ``languages.detect_language`` is unchanged.
    """
    allowed: frozenset[str] | None = None
    if allowlist is not None:
        allowed = frozenset(
            lang for lang, name in _ENG_NAME.items() if name in allowlist
        )

    counters, counts = _raw_detect_script(text)
    main_script, main_count = counters[0]
    if main_count <= 0:
        return None

    kind, payload = _d.SCRIPT_LANG_GROUP[main_script]
    if kind == "one":
        # NOTE: whatlang does not consult the filter list on this path.
        result: tuple[str, float] | None = (payload, 1.0)
    elif kind == "mandarin":
        result = _detect_mandarin(counts, allowed)
    else:
        result = _combined_detect(text, payload, allowed)

    if result is None:
        return None
    lang, confidence = result
    return _ENG_NAME[lang], _as_i32(confidence * 100.0)
