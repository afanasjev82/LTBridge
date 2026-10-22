"""Server configuration dataclass."""

from __future__ import annotations

import os
from dataclasses import dataclass


@dataclass
class Config:
    """Runtime configuration for ltbridge, sourced from CLI arguments."""

    llm_url: str
    host: str = "0.0.0.0"
    port: int = 5050
    char_limit: int = 5000
    api_key: str = ""
    llm_api_key: str = ""
    llm_model: str = ""     # empty = auto-resolved from /v1/models
    llm_max_tokens: int = 0
    llm_chars_per_token: float = 0.0
    llm_max_tokens_mult: float = 0.0
    llm_max_tokens_floor: int = 64
    llm_timeout: float = 120.0
    llm_reasoning_effort: str = ""  # empty = `reasoning_effort` omitted from requests
    verbose: bool = False
    # Opt-in (v0.1.5, Python-only): a slow-failure fallback body also carries
    # `"fallback": "llm_timeout" | "llm_error"`. Off = Rust's bytes. Last field,
    # so a positional Config(...) built by an embedder keeps its meaning.
    mark_fallback: bool = False

    @classmethod
    def from_env(cls) -> Config:
        """Rebuild a Config from the LTBRIDGE_* environment variables.

        Used by `app.app_factory()` inside a Granian worker process, which on
        Windows/macOS is spawned via `multiprocessing`'s 'spawn' start method (no
        fork) and therefore cannot receive a live Config/Litestar object across the
        process boundary — see the comment in `__main__.py` next to the Granian(...)
        call for the full explanation. `__main__.py` sets these variables from the
        parsed CLI args before starting Granian.
        """
        return cls(
            llm_url=os.environ["LTBRIDGE_LLM_URL"],
            host=os.environ.get("LTBRIDGE_HOST", "0.0.0.0"),
            port=int(os.environ.get("LTBRIDGE_PORT", "5050")),
            char_limit=int(os.environ.get("LTBRIDGE_CHAR_LIMIT", "5000")),
            api_key=os.environ.get("LTBRIDGE_API_KEY", ""),
            llm_api_key=os.environ.get("LTBRIDGE_LLM_API_KEY", ""),
            llm_model=os.environ.get("LTBRIDGE_LLM_MODEL", ""),
            llm_max_tokens=int(os.environ.get("LTBRIDGE_LLM_MAX_TOKENS", "0")),
            llm_chars_per_token=float(os.environ.get("LTBRIDGE_LLM_CHARS_PER_TOKEN", "0.0")),
            llm_max_tokens_mult=float(os.environ.get("LTBRIDGE_LLM_MAX_TOKENS_MULT", "0.0")),
            llm_max_tokens_floor=int(os.environ.get("LTBRIDGE_LLM_MAX_TOKENS_FLOOR", "64")),
            llm_timeout=float(os.environ.get("LTBRIDGE_LLM_TIMEOUT", "120.0")),
            llm_reasoning_effort=os.environ.get("LTBRIDGE_LLM_REASONING_EFFORT", ""),
            verbose=os.environ.get("LTBRIDGE_VERBOSE", "") == "1",
            mark_fallback=os.environ.get("LTBRIDGE_MARK_FALLBACK", "") == "1",
        )
