//! Faithful PyO3 port of `.rust-ref/ltengine/src/languages.rs`'s whatlang
//! detector construction. This crate exposes exactly one function, `detect`,
//! whose allowlist, Mandarin->Chinese remap, and confidence math are copied
//! line-by-line from languages.rs's `LANGUAGES` static and `detect_lang` fn
//! (not from any simplified sketch) so that Python-side detection is
//! byte-identical to the Rust oracle.
//!
//! languages.rs builds an intermediate `eng_name -> &Lang` map over ALL of
//! `Lang::all()` (with "Mandarin" remapped to "Chinese"), then looks up each
//! of its 49 supported display names in that map to decide which `Lang`s are
//! detectable and to associate each with its "our" display name. That is
//! reproduced verbatim below, rather than the brief's simpler `.filter()`
//! sketch, even though the two are provably equivalent here: every one of
//! whatlang 0.18's 70 `Lang` variants has a distinct `eng_name()` (verified
//! against the vendored `whatlang-0.18.0/src/lang.rs`), so there is no
//! multi-key collision for the map to resolve differently than a filter
//! would, and `Detector`/`FilterList::is_allowed` treats the allowlist as a
//! pure set (`Vec::contains`) — allowlist *order* never affects which
//! language wins (confirmed against `whatlang-0.18.0/src/core/filter_list.rs`
//! and `src/trigrams/detection.rs`, whose scoring iterates a fixed static
//! per-script language-profile list, not our allowlist).
use std::collections::HashMap;

use pyo3::prelude::*;
use whatlang::{Detector, Lang};

/// The 49 "our" display names, in the exact order of `LANGS` in
/// languages.rs (its third tuple element). Two of these — "Norwegian" and
/// "Slovenian" — do NOT match any whatlang `eng_name()` (whatlang calls them
/// "Bokmal" and "Slovene" respectively), and "Chinese (traditional)" /
/// "Portuguese (Brazil)" have no distinct whatlang variant either: all four
/// are therefore never detectable, exactly as in the oracle, where their
/// `Language.lang_detect` is `None`. This is intentional parity, not a bug.
const SUPPORTED: &[&str] = &[
    "English", "Albanian", "Arabic", "Azerbaijani", "Basque", "Bengali", "Bulgarian",
    "Catalan", "Chinese", "Chinese (traditional)", "Czech", "Danish", "Dutch",
    "Esperanto", "Estonian", "Finnish", "French", "Galician", "German", "Greek",
    "Hebrew", "Hindi", "Hungarian", "Indonesian", "Irish", "Italian", "Japanese",
    "Korean", "Latvian", "Lithuanian", "Malay", "Norwegian", "Persian", "Polish",
    "Portuguese", "Portuguese (Brazil)", "Romanian", "Russian", "Serbian", "Slovak",
    "Slovenian", "Spanish", "Swedish", "Tagalog", "Thai", "Turkish", "Ukrainian",
    "Urdu", "Vietnamese",
];

/// languages.rs's `eng_name_map`: from whatlang names to our names. The only
/// entry in the oracle is `Mandarin -> Chinese`.
fn remap(name: &str) -> &str {
    if name == "Mandarin" {
        "Chinese"
    } else {
        name
    }
}

/// Mirrors languages.rs's `LANGUAGES` construction: build `eng_name -> Lang`
/// over all of `Lang::all()` (remapped), then keep only the `Lang`s whose
/// (remapped) name is one of our 49 supported display names, in `SUPPORTED`
/// order (mirrors `LANGUAGES.iter().filter_map(|l| l.lang_detect.copied())`,
/// which iterates in `LANGS` order).
fn allowlist() -> Vec<Lang> {
    let mut by_name: HashMap<&'static str, Lang> = HashMap::new();
    for &lang in Lang::all() {
        by_name.insert(remap(lang.eng_name()), lang);
    }
    SUPPORTED
        .iter()
        .filter_map(|name| by_name.get(name).copied())
        .collect()
}

/// Detect the language of `text`, restricted to the allowlist above.
///
/// Returns `(english_name_after_mandarin_remap, confidence_0_100_truncated)`,
/// mirroring languages.rs's `detect_lang`: `(info.confidence() * 100.0) as
/// i32` (truncating `as` cast, not rounding). `None` on no detection, exactly
/// like the oracle's `detector.detect(q)` returning `None`.
#[pyfunction]
fn detect(text: &str) -> Option<(String, i32)> {
    let detector = Detector::with_allowlist(allowlist());
    detector.detect(text).map(|info| {
        (
            remap(info.lang().eng_name()).to_string(),
            (info.confidence() * 100.0) as i32,
        )
    })
}

#[pymodule]
fn whatlang_py(m: &Bound<'_, PyModule>) -> PyResult<()> {
    m.add_function(wrap_pyfunction!(detect, m)?)?;
    Ok(())
}
