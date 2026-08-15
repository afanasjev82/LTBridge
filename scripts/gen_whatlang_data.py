"""Generate ``backend/src/ltbridge/core/_whatlang_data.py`` from the whatlang crate.

Everything the pure-Python detector (``ltbridge.core.whatlang``) needs as *data*
is parsed straight out of the ``whatlang`` Rust crate's own source files — the
trigram profiles, the per-script Unicode range tables, the alphabet strings, the
``Lang``/``Script`` metadata and the script->language mappings. Nothing is
hand-transcribed, so a crate bump is a re-run of this script plus a re-run of
``scripts/sweep_detection_parity.py``.

Two tables do NOT come from the crate: ``char::to_lowercase`` and the
``Cased``/``Case_Ignorable`` derived properties that drive the Greek final-sigma
rule. whatlang lowercases via ``str::to_lowercase``, i.e. the Rust *standard
library's* Unicode tables, which are a different Unicode version from CPython's
(rustc 1.96 ships Unicode 16.0; CPython 3.12 ships 15.0 — they disagree on 55
code points, 8 of them inside whatlang's own Latin range). Snapshotting the Rust
tables therefore does two things: it keeps detection byte-identical to the Rust
oracle, and it pins detection against future CPython Unicode bumps. They are
obtained by compiling a tiny throwaway Rust program with ``rustc`` into a temp
directory (no Rust source is kept in this repository); pass ``--case-tables`` to
reuse a previously dumped TSV instead.

Usage::

    uv run python scripts/gen_whatlang_data.py                # auto-locate crate
    uv run python scripts/gen_whatlang_data.py --crate-dir ~/.cargo/registry/src/*/whatlang-0.18.0
"""

from __future__ import annotations

import argparse
import glob
import hashlib
import os
import re
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
OUT_PATH = ROOT / "backend" / "src" / "ltbridge" / "core" / "_whatlang_data.py"

# ---------------------------------------------------------------------------
# Rust source snippets we parse
# ---------------------------------------------------------------------------

PARSED_FILES = (
    "src/lang.rs",
    "src/utils.rs",
    "src/scripts/chars.rs",
    "src/scripts/detect.rs",
    "src/scripts/grouping.rs",
    "src/scripts/lang_mapping.rs",
    "src/alphabets/detection.rs",
    "src/alphabets/latin.rs",
    "src/alphabets/cyrillic.rs",
    "src/trigrams/mod.rs",
    "src/trigrams/profiles.rs",
)

_CHAR_LIT = r"'(?:\\u\{([0-9A-Fa-f]{1,6})\}|\\(.)|(.))'"


def _rust_char(match: re.Match[str]) -> str:
    """Decode one Rust ``char`` literal from a match of ``_CHAR_LIT``."""
    hexcode, escaped, plain = match.group(1), match.group(2), match.group(3)
    if hexcode is not None:
        return chr(int(hexcode, 16))
    if escaped is not None:
        return {"n": "\n", "r": "\r", "t": "\t", "0": "\0", "\\": "\\", "'": "'", '"': '"'}[
            escaped
        ]
    assert plain is not None
    return plain


def _parse_ranges(body: str) -> tuple[tuple[int, int], ...]:
    """Parse a ``matches!`` body of ``'a'..='z' | '\\u{80}'`` patterns into code-point ranges."""
    ranges: list[tuple[int, int]] = []
    pos = 0
    pat_range = re.compile(_CHAR_LIT + r"\s*\.\.=\s*" + _CHAR_LIT)
    pat_one = re.compile(_CHAR_LIT)
    while pos < len(body):
        m = pat_range.search(body, pos)
        m1 = pat_one.search(body, pos)
        if m1 is None:
            break
        if m is not None and m.start() == m1.start():
            both = list(pat_one.finditer(m.group(0)))
            assert len(both) == 2, m.group(0)
            ranges.append((ord(_rust_char(both[0])), ord(_rust_char(both[1]))))
            pos = m.end()
        else:
            cp = ord(_rust_char(m1))
            ranges.append((cp, cp))
            pos = m1.end()
    return tuple(ranges)


# ---------------------------------------------------------------------------
# Individual parsers
# ---------------------------------------------------------------------------


def parse_lang(src: str) -> tuple[list[str], dict[str, int], dict[str, str]]:
    """``Lang::all()`` order, enum discriminants and ``eng_name`` for every variant."""
    enum_body = re.search(r"pub enum Lang \{(.*?)\n\}", src, re.DOTALL)
    assert enum_body, "Lang enum not found"
    discriminants = {
        name: int(value)
        for name, value in re.findall(r"^\s{4}(\w+) = (\d+),", enum_body.group(1), re.MULTILINE)
    }

    values_body = re.search(r"const VALUES: \[Lang; (\d+)\] = \[(.*?)\n\];", src, re.DOTALL)
    assert values_body, "Lang VALUES not found"
    order = re.findall(r"Lang::(\w+)", values_body.group(2))
    assert len(order) == int(values_body.group(1)) == len(discriminants)

    eng_body = re.search(r"fn lang_to_eng_name\(lang: Lang\) -> &'static str \{(.*?)\n\}", src, re.DOTALL)
    assert eng_body, "lang_to_eng_name not found"
    eng_names = dict(re.findall(r'Lang::(\w+) => "([^"]+)",', eng_body.group(1)))
    assert set(eng_names) == set(order)
    return order, discriminants, eng_names


def parse_stop_chars(src: str) -> tuple[tuple[int, int], ...]:
    body = re.search(r"pub fn is_stop_char\(ch: char\) -> bool \{\s*matches!\((.*?)\)\s*\}", src, re.DOTALL)
    assert body, "is_stop_char not found"
    return _parse_ranges(body.group(1))


def parse_script_char_ranges(src: str) -> dict[str, tuple[tuple[int, int], ...]]:
    out: dict[str, tuple[tuple[int, int], ...]] = {}
    for name, body in re.findall(
        r"fn is_(\w+)\(ch: char\) -> bool \{\s*matches!\(ch,(.*?)\)\s*\n\}", src, re.DOTALL
    ):
        out[name] = _parse_ranges(body)
    assert len(out) == 25, f"expected 25 script predicates, got {len(out)}"
    return out


def parse_script_counter_order(src: str) -> list[tuple[str, str]]:
    body = re.search(r"let mut script_counters: \[ScriptCounter; (\d+)\] = \[(.*?)\n    \];", src, re.DOTALL)
    assert body, "script_counters array not found"
    pairs = re.findall(r"\(Script::(\w+), chars::is_(\w+), 0\)", body.group(2))
    assert len(pairs) == int(body.group(1))
    return pairs


def parse_script_lang_group(src: str) -> dict[str, tuple[str, str]]:
    body = re.search(r"fn to_lang_group\(self\) -> ScriptLangGroup \{.*?match self \{(.*?)\n        \}", src, re.DOTALL)
    assert body, "to_lang_group not found"
    out: dict[str, tuple[str, str]] = {}
    for line in body.group(1).splitlines():
        line = line.strip()
        if not line or line.startswith("//"):
            continue
        m = re.match(r"((?:Script::\w+\s*\|?\s*)+)=>\s*(.*?),?$", line)
        if not m:
            continue
        scripts = re.findall(r"Script::(\w+)", m.group(1))
        target = m.group(2).rstrip(",")
        if target == "Mandarin":
            value = ("mandarin", "")
        elif target.startswith("Multi("):
            value = ("multi", re.search(r"MLS::(\w+)", target).group(1))  # type: ignore[union-attr]
        elif target.startswith("One("):
            value = ("one", re.search(r"Lang::(\w+)", target).group(1))  # type: ignore[union-attr]
        else:
            raise AssertionError(f"unparsed to_lang_group arm: {line}")
        for script in scripts:
            out[script] = value
    assert len(out) == 25, f"expected 25 lang-group arms, got {len(out)}"
    return out


def parse_script_langs(src: str) -> dict[str, tuple[str, ...]]:
    consts = {
        name: tuple(re.findall(r"Lang::(\w+)", body))
        for name, count, body in re.findall(
            r"const (\w+): \[Lang; (\d+)\] = \[(.*?)\];", src, re.DOTALL
        )
    }
    body = re.search(r"pub fn script_langs\(script: Script\) -> &'static \[Lang\] \{.*?match script \{(.*?)\n    \}", src, re.DOTALL)
    assert body, "script_langs not found"
    out: dict[str, tuple[str, ...]] = {}
    for line in body.group(1).splitlines():
        line = line.strip()
        if not line or line.startswith("//"):
            continue
        m = re.match(r"((?:Script::\w+\s*\|?\s*)+)=>\s*(.*?),?$", line)
        if not m:
            continue
        scripts = re.findall(r"Script::(\w+)", m.group(1))
        target = m.group(2).rstrip(",")
        if target.startswith("&["):
            langs = tuple(re.findall(r"Lang::(\w+)", target))
        else:
            langs = consts[target.lstrip("&")]
        for script in scripts:
            out[script] = langs
    assert len(out) == 25, f"expected 25 script_langs arms, got {len(out)}"
    return out


def parse_alphabets(latin_src: str, cyrillic_src: str) -> dict[str, tuple[tuple[str, str], ...]]:
    out: dict[str, tuple[tuple[str, str], ...]] = {}
    for script, src, const_name in (
        ("Latin", latin_src, "LATIN_ALPHABETS"),
        ("Cyrillic", cyrillic_src, "CYRILLIC_ALPHABETS"),
    ):
        strings = dict(re.findall(r'const (\w+): &str =\s*"([^"]*)";', src, re.DOTALL))
        body = re.search(rf"const {const_name}: &\[\(Lang, &str\)\] = &\[(.*?)\n\];", src, re.DOTALL)
        assert body, f"{const_name} not found"
        pairs = re.findall(r"\(Lang::(\w+), (\w+)\)", body.group(1))
        out[script] = tuple((lang, strings[const]) for lang, const in pairs)
    return out


def parse_mock_alphabet_langs(src: str) -> dict[str, tuple[str, ...]]:
    """The Arabic/Devanagari/Hebrew ``build_mock`` lang lists in alphabets/detection.rs."""
    out: dict[str, tuple[str, ...]] = {}
    for script, langs in re.findall(r"MLS::(\w+) => build_mock\(vec!\[([^\]]*)\]", src):
        out[script] = tuple(re.findall(r"Lang::(\w+)", langs))
    assert set(out) == {"Arabic", "Devanagari", "Hebrew"}, out.keys()
    return out


def parse_trigram_constants(src: str) -> dict[str, int]:
    out: dict[str, int] = {}
    for name, expr in re.findall(r"pub const (\w+): (?:u32|usize) = ([^;]+);", src):
        expr = expr.strip()
        out[name] = int(expr) if expr.isdigit() else eval(expr, {}, dict(out))
    assert out["MAX_TRIGRAM_DISTANCE"] == 300
    return out


def parse_trigram_profiles(src: str) -> dict[str, tuple[tuple[str, str], ...]]:
    """script -> ((lang, 900-char blob of 300 concatenated trigrams), ...)."""
    out: dict[str, tuple[tuple[str, str], ...]] = {}
    trigram_re = re.compile(r"Trigram\(" + _CHAR_LIT + r", " + _CHAR_LIT + r", " + _CHAR_LIT + r"\)")
    lit_re = re.compile(_CHAR_LIT)
    for script_const, body in re.findall(
        r"pub static (\w+)_LANGS: LangProfileList = &\[(.*?)\n\];", src, re.DOTALL
    ):
        script = script_const.capitalize()
        profiles: list[tuple[str, str]] = []
        for lang, entries in re.findall(r"Lang::(\w+),\s*&\[(.*?)\n        \],", body, re.DOTALL):
            blob_parts: list[str] = []
            for m in trigram_re.finditer(entries):
                chars = [_rust_char(lm) for lm in lit_re.finditer(m.group(0))]
                assert len(chars) == 3
                blob_parts.append("".join(chars))
            assert len(blob_parts) == 300, f"{lang}: {len(blob_parts)} trigrams"
            profiles.append((lang, "".join(blob_parts)))
        out[script] = tuple(profiles)
    assert sum(len(v) for v in out.values()) == 51, out.keys()
    return out


# ---------------------------------------------------------------------------
# Rust std case tables (lowercase map + Cased / Case_Ignorable)
# ---------------------------------------------------------------------------

_CASE_PROBE_RS = """
use std::io::{self, Write, BufWriter};

const SIGMA: char = '\\u{03A3}';
const FINAL_SIGMA: char = '\\u{03C2}';

// `Cased` and `Case_Ignorable` are private to core::unicode, so they are
// recovered behaviourally from the one place `str::to_lowercase` consults
// them: the Final_Sigma context rule.
//   A(c) = lower("A" + c + "S") ends in final sigma  <=>  CI(c) || Cased(c)
//   B(c) = lower(      c + "S") ends in final sigma  <=> !CI(c) && Cased(c)
// hence CI(c) = A(c) && !B(c), and for non-CI chars Cased(c) = B(c).
fn main() {
    let stdout = io::stdout();
    let mut out = BufWriter::new(stdout.lock());
    for cp in 0u32..0x110000u32 {
        if let Some(c) = char::from_u32(cp) {
            let one = c.to_string();
            let low: String = one.to_lowercase();
            let mut sa = String::from("A");
            sa.push(c);
            sa.push(SIGMA);
            let a = sa.to_lowercase().chars().last() == Some(FINAL_SIGMA);
            let mut sb = String::new();
            sb.push(c);
            sb.push(SIGMA);
            let b = sb.to_lowercase().chars().last() == Some(FINAL_SIGMA);
            let ci = a && !b;
            let cased = b;
            let mapped = low != one;
            if mapped || ci || cased {
                let codes: Vec<String> =
                    low.chars().map(|x| format!("{:X}", x as u32)).collect();
                let m = if mapped { codes.join(",") } else { String::from("-") };
                writeln!(out, "{:X}\\t{}\\t{}\\t{}", cp, m, ci as u8, cased as u8).unwrap();
            }
        }
    }
}
"""


def dump_case_tables() -> str:
    """Compile+run the probe above with ``rustc`` and return its TSV output."""
    if shutil.which("rustc") is None:
        sys.exit(
            "ERROR: rustc not found. Install a Rust toolchain, or pass --case-tables "
            "with a previously dumped TSV."
        )
    with tempfile.TemporaryDirectory() as td:
        rs = Path(td) / "case_probe.rs"
        rs.write_text(_CASE_PROBE_RS, encoding="utf-8")
        exe = Path(td) / "case_probe"
        subprocess.run(["rustc", "-O", "-o", str(exe), str(rs)], check=True)
        proc = subprocess.run([str(exe)], check=True, capture_output=True, text=True)
    return proc.stdout


def parse_case_tables(tsv: str) -> tuple[dict[str, str], list[tuple[int, int]], list[tuple[int, int]]]:
    lower: dict[str, str] = {}
    ci_points: list[int] = []
    cased_points: list[int] = []
    for line in tsv.splitlines():
        if not line or line.startswith("#"):
            continue
        cp_hex, mapped, ci, cased = line.split("\t")
        cp = int(cp_hex, 16)
        if mapped != "-":
            lower[chr(cp)] = "".join(chr(int(c, 16)) for c in mapped.split(","))
        if ci == "1":
            ci_points.append(cp)
        if cased == "1":
            cased_points.append(cp)
    assert lower, "empty lowercase table"
    return lower, _to_ranges(ci_points), _to_ranges(cased_points)


def _to_ranges(points: list[int]) -> list[tuple[int, int]]:
    ranges: list[tuple[int, int]] = []
    for cp in points:
        if ranges and cp == ranges[-1][1] + 1:
            ranges[-1] = (ranges[-1][0], cp)
        else:
            ranges.append((cp, cp))
    return ranges


# ---------------------------------------------------------------------------
# Emission
# ---------------------------------------------------------------------------


def _fmt_ranges(ranges: tuple[tuple[int, int], ...] | list[tuple[int, int]]) -> str:
    return "(\n" + "".join(f"    (0x{lo:04X}, 0x{hi:04X}),\n" for lo, hi in ranges) + ")"


def _fmt_str_tuple(items: tuple[str, ...] | list[str]) -> str:
    return "(" + ", ".join(repr(i) for i in items) + ("," if len(items) == 1 else "") + ")"


def emit(data: dict[str, object]) -> str:
    lines: list[str] = []
    w = lines.append
    w('"""Detection tables generated from the whatlang Rust crate. DO NOT EDIT.')
    w("")
    w(f"Source crate : {data['crate']} {data['crate_version']}")
    w("Generator    : scripts/gen_whatlang_data.py")
    w(f"Rust std     : {data['rustc']} (LOWERCASE_MAP / CASED / CASE_IGNORABLE only)")
    w("")
    w("SHA-256 of every parsed crate file:")
    for rel, digest in data["checksums"]:  # type: ignore[union-attr]
        w(f"  {digest}  {rel}")
    w("")
    w("Regenerate with `uv run python scripts/gen_whatlang_data.py`, then re-run")
    w("`uv run python scripts/sweep_detection_parity.py` before trusting the result.")
    w('"""')
    w("")
    w("from __future__ import annotations")
    w("")
    w(f"CRATE = {data['crate']!r}")
    w(f"CRATE_VERSION = {data['crate_version']!r}")
    w(f"RUSTC_VERSION = {data['rustc']!r}")
    w("")
    w("# --- Lang metadata (src/lang.rs) ---")
    w(f"LANG_ORDER: tuple[str, ...] = {_fmt_str_tuple(data['lang_order'])}  # type: ignore[arg-type]")
    w("LANG_ENG_NAME: dict[str, str] = {")
    for lang in data["lang_order"]:  # type: ignore[union-attr]
        w(f"    {lang!r}: {data['lang_eng_name'][lang]!r},")  # type: ignore[index]
    w("}")
    w("")
    w("# --- Stop characters (src/utils.rs is_stop_char) ---")
    w(f"STOP_CHAR_RANGES: tuple[tuple[int, int], ...] = {_fmt_ranges(data['stop_ranges'])}")  # type: ignore[arg-type]
    w("")
    w("# --- Per-script Unicode ranges (src/scripts/chars.rs) ---")
    w("SCRIPT_CHAR_RANGES: dict[str, tuple[tuple[int, int], ...]] = {")
    for script, ranges in data["script_char_ranges"]:  # type: ignore[union-attr]
        w(f"    {script!r}: {_fmt_ranges(ranges)},")
    w("}")
    w("")
    w("# --- Script counter order (src/scripts/detect.rs raw_detect_script) ---")
    w(f"SCRIPT_COUNTER_ORDER: tuple[str, ...] = {_fmt_str_tuple(data['script_counter_order'])}  # type: ignore[arg-type]")
    w("")
    w("# --- Script -> language group (src/scripts/grouping.rs) ---")
    w("SCRIPT_LANG_GROUP: dict[str, tuple[str, str]] = {")
    for script in data["script_counter_order"]:  # type: ignore[union-attr]
        kind, payload = data["script_lang_group"][script]  # type: ignore[index]
        w(f"    {script!r}: ({kind!r}, {payload!r}),")
    w("}")
    w("")
    w("# --- Script -> languages (src/scripts/lang_mapping.rs) ---")
    w("SCRIPT_LANGS: dict[str, tuple[str, ...]] = {")
    for script in data["script_counter_order"]:  # type: ignore[union-attr]
        w(f"    {script!r}: {_fmt_str_tuple(data['script_langs'][script])},")  # type: ignore[index]
    w("}")
    w("")
    w("# --- Alphabets (src/alphabets/{latin,cyrillic}.rs) ---")
    w("ALPHABETS: dict[str, tuple[tuple[str, str], ...]] = {")
    for script, pairs in data["alphabets"]:  # type: ignore[union-attr]
        w(f"    {script!r}: (")
        for lang, alphabet in pairs:
            w(f"        ({lang!r}, {alphabet!r}),")
        w("    ),")
    w("}")
    w("")
    w("# --- build_mock() language lists (src/alphabets/detection.rs) ---")
    w("MOCK_ALPHABET_LANGS: dict[str, tuple[str, ...]] = {")
    for script, langs in sorted(data["mock_alphabet_langs"].items()):  # type: ignore[union-attr]
        w(f"    {script!r}: {_fmt_str_tuple(langs)},")
    w("}")
    w("")
    w("# --- Trigram constants (src/trigrams/mod.rs) ---")
    for name, value in sorted(data["trigram_constants"].items()):  # type: ignore[union-attr]
        w(f"{name} = {value}")
    w("")
    w("# --- Trigram profiles (src/trigrams/profiles.rs) ---")
    w("# Each profile is 300 trigrams flattened into one 900-character string,")
    w("# in the crate's profile order (index == the trigram's rank).")
    w("TRIGRAM_PROFILES: dict[str, tuple[tuple[str, str], ...]] = {")
    for script, profiles in data["trigram_profiles"]:  # type: ignore[union-attr]
        w(f"    {script!r}: (")
        for lang, blob in profiles:
            w(f"        ({lang!r}, {blob!r}),")
        w("    ),")
    w("}")
    w("")
    w("# --- Rust std case tables (see module docstring) ---")
    w("LOWERCASE_MAP: dict[str, str] = {")
    for src_ch, dst in data["lowercase_map"].items():  # type: ignore[union-attr]
        w(f"    {src_ch!r}: {dst!r},")
    w("}")
    w("")
    w(f"CASE_IGNORABLE_RANGES: tuple[tuple[int, int], ...] = {_fmt_ranges(data['ci_ranges'])}")  # type: ignore[arg-type]
    w("")
    w(f"CASED_RANGES: tuple[tuple[int, int], ...] = {_fmt_ranges(data['cased_ranges'])}")  # type: ignore[arg-type]
    w("")
    return "\n".join(lines)


# ---------------------------------------------------------------------------


def locate_crate() -> Path:
    patterns = [
        os.path.expanduser("~/.cargo/registry/src/*/whatlang-*"),
        str(ROOT / ".rust-ref" / "vendor" / "whatlang-*"),
    ]
    for pattern in patterns:
        hits = sorted(glob.glob(pattern))
        if hits:
            return Path(hits[-1])
    sys.exit("ERROR: could not locate the whatlang crate source; pass --crate-dir.")


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--crate-dir", type=Path, default=None)
    ap.add_argument("--case-tables", type=Path, default=None)
    ap.add_argument("--out", type=Path, default=OUT_PATH)
    args = ap.parse_args()

    crate = args.crate_dir or locate_crate()
    print(f"crate: {crate}")
    version = crate.name.split("-", 1)[1]

    def read(rel: str) -> str:
        return (crate / rel).read_text(encoding="utf-8")

    checksums = [
        (rel, hashlib.sha256((crate / rel).read_bytes()).hexdigest()) for rel in PARSED_FILES
    ]

    lang_order, _discriminants, lang_eng_name = parse_lang(read("src/lang.rs"))
    assert len(set(lang_eng_name.values())) == len(lang_eng_name), (
        "eng_name collision: the allowlist can no longer be keyed by display name"
    )

    script_counter_order = parse_script_counter_order(read("src/scripts/detect.rs"))
    char_ranges = parse_script_char_ranges(read("src/scripts/chars.rs"))
    script_char_ranges = [(script, char_ranges[fn]) for script, fn in script_counter_order]

    if args.case_tables:
        tsv = args.case_tables.read_text(encoding="utf-8")
        rustc = "supplied via --case-tables"
    else:
        tsv = dump_case_tables()
        rustc = subprocess.run(
            ["rustc", "--version"], check=True, capture_output=True, text=True
        ).stdout.strip()
    lowercase_map, ci_ranges, cased_ranges = parse_case_tables(tsv)

    data: dict[str, object] = {
        "crate": "whatlang",
        "crate_version": version,
        "rustc": rustc,
        "checksums": checksums,
        "lang_order": lang_order,
        "lang_eng_name": lang_eng_name,
        "stop_ranges": parse_stop_chars(read("src/utils.rs")),
        "script_char_ranges": script_char_ranges,
        "script_counter_order": [s for s, _ in script_counter_order],
        "script_lang_group": parse_script_lang_group(read("src/scripts/grouping.rs")),
        "script_langs": parse_script_langs(read("src/scripts/lang_mapping.rs")),
        "alphabets": list(
            parse_alphabets(read("src/alphabets/latin.rs"), read("src/alphabets/cyrillic.rs")).items()
        ),
        "mock_alphabet_langs": parse_mock_alphabet_langs(read("src/alphabets/detection.rs")),
        "trigram_constants": parse_trigram_constants(read("src/trigrams/mod.rs")),
        "trigram_profiles": list(parse_trigram_profiles(read("src/trigrams/profiles.rs")).items()),
        "lowercase_map": lowercase_map,
        "ci_ranges": ci_ranges,
        "cased_ranges": cased_ranges,
    }

    args.out.write_text(emit(data), encoding="utf-8")
    print(f"wrote {args.out} ({args.out.stat().st_size} bytes)")
    print(f"  langs={len(lang_order)} scripts={len(script_counter_order)}")
    print(f"  lowercase entries={len(lowercase_map)} ci_ranges={len(ci_ranges)} cased_ranges={len(cased_ranges)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
