"""Type stub for the vendored `whatlang_py` PyO3 extension (see src/lib.rs)."""

def detect(text: str) -> tuple[str, int] | None:
    """Detect the language of *text*, restricted to languages.rs's supported allowlist.

    Returns ``(english_display_name_after_mandarin_remap, confidence_0_100_truncated)``,
    or ``None`` if whatlang could not detect a language.
    """
