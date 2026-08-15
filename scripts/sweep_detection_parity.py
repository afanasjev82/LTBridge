"""Zero-divergence gate for the pure-Python whatlang port.

Compares :func:`ltbridge.core.whatlang.detect` against an *oracle* on a large
generated corpus and prints a divergence count that must be ``0``.

Two oracle modes:

``--oracle recorded`` (default)
    Replays ``tests/fixtures/detection_sweep.json.gz`` — the corpus plus the
    outputs the vendored ``whatlang_py`` PyO3 binding produced while it still
    existed. This is what ``tests/unit/test_detection_sweep.py`` runs, so
    parity stays regression guarded now that the binding is gone.

``--oracle binding``
    Calls the binding directly. This is how the port was *proved* before the
    Rust crate was deleted, and how expectations are re-recorded after a crate
    bump — it requires reinstating ``whatlang-py`` as a workspace member first
    (see the README).

Corpus (deterministic, seeded):

* the 26 texts the differential ``/detect`` fixtures fire at the Rust server;
* every ``bench/texts.py`` entry;
* natural-language string literals harvested from the whatlang crate's own
  tests (when ``--crate-dir`` resolves), covering all five multi-language
  scripts;
* random substrings (length 1..200) of all of the above;
* mixed-script concatenations, including Greek fragments spliced into Latin
  text so the ``Final_Sigma`` lowercasing rule is exercised;
* digit / punctuation / emoji / whitespace-only inputs;
* every single character in the Latin, Cyrillic, Arabic, Hebrew, Devanagari and
  Greek range tables, plus sampled characters and range boundaries for the
  other 19 scripts, plus code points that CPython and rustc disagree about.
"""

from __future__ import annotations

import argparse
import gzip
import json
import random
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "backend" / "src"))
sys.path.insert(0, str(ROOT))

from ltbridge.core import _whatlang_data as D
from ltbridge.core import whatlang
from ltbridge.core.languages import _LANG_DEFS

DEFAULT_DUMP = ROOT / "tests" / "fixtures" / "detection_sweep.json.gz"

ALLOWLIST = frozenset(name for _code, _alias, name in _LANG_DEFS)

# The texts tests/differential/test_detect_parity.py fires at the Rust oracle.
FIXTURE_TEXTS = [
    "Hello, how are you today?",
    "Привет, как дела?",
    "Bonjour tout le monde",
    "Hallo, wie geht es dir?",
    "Hola, ¿cómo estás?",
    "Ciao, come stai?",
    "こんにちは、お元気ですか",
    "안녕하세요",
    "你好，世界",
    "مرحبا بالعالم",
    "Tere, kuidas läheb?",
    "Cześć, jak się masz?",
    "Merhaba dünya",
    "Xin chào thế giới",
    "สวัสดีครับ",
    "Γειά σου κόσμε",
    "שלום עולם",
    "Hej, hur mår du?",
    "Ahoj, jak se máš?",
    "Salut, ce mai faci?",
    "ok",
    "123",
    "a",
    "",
    " ",
    "Привет, мир",
]

# Full-range single-character coverage for the scripts that actually reach the
# trigram/alphabet path; the rest are sampled (Hangul alone is 11k code points).
FULL_COVERAGE_SCRIPTS = ("Latin", "Cyrillic", "Arabic", "Hebrew", "Devanagari", "Greek")
SAMPLES_PER_SCRIPT = 60

# Code points where rustc 1.96 (Unicode 16.0) and CPython 3.12 (15.0) disagree
# about `to_lowercase`; the port must follow rustc.
UNICODE_DRIFT_POINTS = (
    0x1C89,
    0xA7CB,
    0xA7CC,
    0xA7CE,
    0xA7D2,
    0xA7D4,
    0xA7DA,
    0xA7DC,
    0x10D50,
    0x10D65,
    0x16EA0,
)


def harvest_crate_samples(crate_dir: Path | None) -> list[str]:
    """Natural-language string literals from the whatlang crate's own tests."""
    if crate_dir is None or not crate_dir.is_dir():
        return []
    out: list[str] = []
    seen: set[str] = set()
    for path in sorted(crate_dir.rglob("*.rs")):
        text = path.read_text(encoding="utf-8", errors="replace")
        for literal in re.findall(r'"((?:[^"\\\n]|\\.){8,})"', text):
            if "\\" in literal or "::" in literal or "{" in literal or "//" in literal:
                continue
            non_ascii = any(ord(c) > 0x7F for c in literal)
            if not non_ascii and literal.count(" ") < 2:
                continue  # ASCII identifiers / paths / format strings
            if literal not in seen:
                seen.add(literal)
                out.append(literal)
    return out


def script_chars(script: str, rng: random.Random) -> list[str]:
    ranges = D.SCRIPT_CHAR_RANGES[script]
    points: list[int] = []
    if script in FULL_COVERAGE_SCRIPTS:
        for lo, hi in ranges:
            points.extend(range(lo, hi + 1))
    else:
        for lo, hi in ranges:
            points.extend({lo, hi, (lo + hi) // 2})
            span = hi - lo + 1
            points.extend(rng.sample(range(lo, hi + 1), min(SAMPLES_PER_SCRIPT, span)))
    return [chr(cp) for cp in sorted(set(points)) if not 0xD800 <= cp <= 0xDFFF]


def build_corpus(crate_dir: Path | None, seed: int) -> list[str]:
    rng = random.Random(seed)
    cases: list[str] = []
    seen: set[str] = set()

    def add(text: str) -> None:
        if text not in seen:
            seen.add(text)
            cases.append(text)

    for text in FIXTURE_TEXTS:
        add(text)

    sys.path.insert(0, str(ROOT))
    from bench.texts import TEXTS as BENCH_TEXTS

    for text in BENCH_TEXTS:
        add(text)

    samples = harvest_crate_samples(crate_dir)
    for text in samples:
        add(text)

    # Per-language material for all 51 profiled languages across all five
    # multi-language scripts: real trigrams from that language's own profile,
    # recombined into word-like runs. This is the only source that reaches every
    # profiled language (the crate's tests cover barely a dozen).
    profile_samples: list[str] = []
    for profiles in D.TRIGRAM_PROFILES.values():
        for _lang, blob in profiles:
            trigrams = [blob[i * 3 : i * 3 + 3] for i in range(len(blob) // 3)]
            for n_trigrams, joiner in ((4, ""), (12, ""), (40, ""), (8, " "), (25, " ")):
                for _ in range(3):
                    picked = [trigrams[rng.randrange(len(trigrams))] for _ in range(n_trigrams)]
                    profile_samples.append(joiner.join(picked).strip())
    for text in profile_samples:
        add(text)

    seeds = FIXTURE_TEXTS + list(BENCH_TEXTS) + samples + profile_samples
    non_empty = [s for s in seeds if s]

    # Random substrings of every seed, lengths 1..200.
    for _ in range(6000):
        base = rng.choice(non_empty)
        length = min(len(base), rng.randint(1, 200))
        start = rng.randint(0, max(0, len(base) - length))
        add(base[start : start + length])

    # Mixed-script concatenations.
    greek = [s for s in non_empty if any(0x370 <= ord(c) <= 0x3FF for c in s)] or [
        "Γειά σου κόσμε",
        "ΟΔΟΣ",
        "ΑΣ",
    ]
    for _ in range(1500):
        a, b = rng.choice(non_empty), rng.choice(non_empty)
        sep = rng.choice(["", " ", ", ", " — "])
        add(a[: rng.randint(1, 40)] + sep + b[: rng.randint(1, 40)])
    for _ in range(600):
        latin = rng.choice(non_empty)
        add(latin[: rng.randint(1, 30)] + rng.choice(["Σ", "ΑΣ", "ΟΔΟΣ", "σΣ", "Σ "]))
        add(rng.choice(greek)[: rng.randint(1, 20)] + latin[: rng.randint(1, 20)])

    # Long texts: the trigram code has three length-dependent branches that
    # short inputs never reach — the >= 300 unique-trigram arm of
    # calculate_distance (no delta subtraction), the 600-trigram truncation in
    # get_trigrams_with_positions, and the 1/3 floor of the alphabet weight.
    for target in (400, 800, 1200, 2000, 3500, 6000):
        for _ in range(12):
            parts: list[str] = []
            size = 0
            while size < target:
                piece = rng.choice(non_empty)
                parts.append(piece)
                size += len(piece) + 1
            add(" ".join(parts)[:target])

    # Mandarin/Japanese ratio grid: the kana share decides Cmn vs Jpn at three
    # thresholds (0.02 / 0.05 / 0.2), and only a swept ratio lands between them.
    han = "県見夜上温国阪題富販水日本語漢字文化中華人民共和"
    kana = "ひらがなカタカナのあいうえおアイウエオ"
    for n_han in (1, 2, 3, 5, 8, 12, 16, 20, 25, 30, 40, 49, 60, 80, 100):
        for n_kana in (0, 1, 2, 3, 4, 5, 8, 12, 20, 40):
            body = "".join(han[i % len(han)] for i in range(n_han))
            body += "".join(kana[i % len(kana)] for i in range(n_kana))
            add(body)
            add(body[::-1])

    # U+1D2B and U+1D78 are the only code points claimed by two script tables
    # (Latin and Cyrillic), so which counter they land in depends on the live,
    # swap-mutated counter order.
    for overlap in ("ᴫ", "ᵸ"):
        add(overlap)
        add(overlap * 5)
        for other in ("abc", "привет", "abcпривет", "приветabc", "ab", "пр"):
            for n in (1, 2, 5, 10):
                add(other + overlap * n)
                add(overlap * n + other)

    # Greek capital sigma inside Latin-dominant text: Final_Sigma decides
    # between 'σ' and 'ς', which changes the trigrams that are scored.
    for host in ("hello world ", "the quick brown fox ", "abc "):
        for tail in ("Σ", "Σ ", "ΣΑ", "ΑΣ", "Σx", "xΣ", "ΑΣ.", "Σ'", "ΣΣ", "ΟΔΟΣ"):
            add(host + tail)
            add(host + tail + " more latin words here to stay latin dominant")

    # Degenerate inputs.
    for text in (
        "",
        " ",
        "  ",
        "\t",
        "\n",
        "\r\n",
        "123",
        "0",
        "3.14159",
        "!!!",
        "...",
        "?!",
        "@#$%^&*()",
        "[]{}|~`",
        "-_=+",
        "😀",
        "😀😀😀",
        "🇬🇧🇺🇸",
        "👨‍👩‍👧‍👦",
        "\u200b",
        " ",
        "﻿",
        "\x00",
        "\x7f",
        "𝕳𝖊𝖑𝖑𝖔",
        "①②③",
        "ﬁﬂ",
        "ß",
        "ẞ",
        "İ",
        "ı",
        "Σ",
        "ς",
        "σ",
    ):
        add(text)

    # Single characters across every script, plus the CPython/rustc drift points.
    for script in D.SCRIPT_COUNTER_ORDER:
        for ch in script_chars(script, rng):
            add(ch)
    # Code points rustc and CPython lowercase differently. A drifted character
    # is absent from every profile either way, but it still *ranks* among the
    # text's trigrams, so it can displace the positions of trigrams that ARE
    # profiled — which is what makes the pinned Rust table observable.
    drift_hosts = (
        "the quick brown fox jumps over the lazy dog",
        "быстрая коричневая лиса прыгает через ленивого пса",
        "Hello, how are you today?",
        "Der schnelle braune Fuchs springt über den faulen Hund.",
        # Vietnamese matters specifically: its profile trigrams live in
        # U+1EA0..U+1EF9, i.e. *between* U+0264 (what rustc lowercases U+A7CB
        # to) and U+A7CB (what CPython 3.12 leaves it as), so the drift moves
        # profiled trigrams' ranks rather than only the drifted one's.
        "Con cáo nâu nhanh nhẹn nhảy qua con chó lười biếng.",
        "Xin chào thế giới, tôi rất vui được gặp bạn hôm nay.",
    )
    for cp in UNICODE_DRIFT_POINTS:
        ch = chr(cp)
        add(ch)
        add("hello " + ch)
        add(ch + " world")
        add("привет " + ch)
        for host in drift_hosts:
            for n in (1, 2, 3, 5, 8, 13):
                add(host + " " + ch * n)
                add(ch * n + " " + host)
                add(host.replace(" ", ch, n))
                add((host + ch) * 2)
                add(host[: 10 + n] + ch + host[10 + n :])

    # Pairs/triples of single characters from different scripts.
    script_pick = {
        script: script_chars(script, rng)[:200] for script in D.SCRIPT_COUNTER_ORDER
    }
    scripts = list(script_pick)
    for _ in range(1500):
        s1, s2 = rng.sample(scripts, 2)
        if not script_pick[s1] or not script_pick[s2]:
            continue
        add(rng.choice(script_pick[s1]) + rng.choice(script_pick[s2]))
        add(rng.choice(script_pick[s1]) * rng.randint(1, 4) + rng.choice(script_pick[s2]))

    return cases


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--oracle", choices=("binding", "recorded"), default="recorded")
    ap.add_argument("--crate-dir", type=Path, default=None)
    ap.add_argument("--seed", type=int, default=20260813)
    ap.add_argument("--dump", type=Path, nargs="?", const=DEFAULT_DUMP, default=None)
    ap.add_argument("--max-report", type=int, default=20)
    args = ap.parse_args()

    if args.dump is not None and args.oracle != "binding":
        ap.error("--dump requires --oracle binding; recording from the recording is circular")

    if args.oracle == "binding":
        crate_dir = args.crate_dir
        if crate_dir is None:
            import glob
            import os

            hits = sorted(glob.glob(os.path.expanduser("~/.cargo/registry/src/*/whatlang-*")))
            crate_dir = Path(hits[-1]) if hits else None
        if args.dump is not None and (crate_dir is None or not crate_dir.is_dir()):
            ap.error(f"--dump requires a valid crate dir; got {crate_dir!r}")
        corpus = build_corpus(crate_dir, args.seed)

        import whatlang_py

        def oracle(text: str) -> tuple[str, int] | None:
            result = whatlang_py.detect(text)
            return (result[0], result[1]) if result is not None else None

        print(f"oracle: whatlang_py binding; crate samples from {crate_dir}")
    else:
        with gzip.open(DEFAULT_DUMP, "rt", encoding="utf-8") as fh:
            recorded = json.load(fh)
        corpus = [case[0] for case in recorded["cases"]]
        expected = {
            case[0]: (tuple(case[1]) if case[1] is not None else None)
            for case in recorded["cases"]
        }

        def oracle(text: str) -> tuple[str, int] | None:
            return expected[text]  # type: ignore[return-value]

        print(f"oracle: recorded {DEFAULT_DUMP} ({recorded['meta']})")

    divergences: list[tuple[str, object, object]] = []
    results: list[tuple[str, tuple[str, int] | None]] = []
    for text in corpus:
        want = oracle(text)
        got = whatlang.detect(text, ALLOWLIST)
        results.append((text, want))
        if want != got:
            divergences.append((text, want, got))

    print(f"cases: {len(corpus)}")
    print(f"divergences: {len(divergences)}")
    for text, want, got in divergences[: args.max_report]:
        print(f"  {text!r}\n    oracle={want}  port={got}")
    if len(divergences) > args.max_report:
        print(f"  ... and {len(divergences) - args.max_report} more")

    if args.dump is not None and not divergences:
        args.dump.parent.mkdir(parents=True, exist_ok=True)
        payload = {
            "meta": {
                "crate": D.CRATE,
                "crate_version": D.CRATE_VERSION,
                "rustc": D.RUSTC_VERSION,
                "seed": args.seed,
                "allowlist_size": len(ALLOWLIST),
                "generator": "scripts/sweep_detection_parity.py",
            },
            "cases": [[text, list(want) if want is not None else None] for text, want in results],
        }
        with gzip.open(args.dump, "wt", encoding="utf-8", compresslevel=9) as fh:
            json.dump(payload, fh, ensure_ascii=False, separators=(",", ":"))
        print(f"recorded {len(results)} cases -> {args.dump} ({args.dump.stat().st_size} bytes)")

    return 1 if divergences else 0


if __name__ == "__main__":
    raise SystemExit(main())
