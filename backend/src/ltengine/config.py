"""Server configuration dataclass."""

from __future__ import annotations

import os
from dataclasses import dataclass


@dataclass
class Config:
    """Runtime configuration for ltengine, sourced from CLI arguments."""

    llm_url: str
    host: str = "0.0.0.0"
    port: int = 5050
    char_limit: int = 5000
    api_key: str = ""
    llm_api_key: str = ""
    llm_model: str = ""     # empty = auto-resolved from /v1/models
    verbose: bool = False

    @classmethod
    def from_env(cls) -> Config:
        """Rebuild a Config from the LTENGINE_* environment variables.

        Used by `app.app_factory()` inside a Granian worker process, which on
        Windows/macOS is spawned via `multiprocessing`'s 'spawn' start method (no
        fork) and therefore cannot receive a live Config/Litestar object across the
        process boundary — see the comment in `__main__.py` next to the Granian(...)
        call for the full explanation. `__main__.py` sets these variables from the
        parsed CLI args before starting Granian.
        """
        return cls(
            llm_url=os.environ["LTENGINE_LLM_URL"],
            host=os.environ.get("LTENGINE_HOST", "0.0.0.0"),
            port=int(os.environ.get("LTENGINE_PORT", "5050")),
            char_limit=int(os.environ.get("LTENGINE_CHAR_LIMIT", "5000")),
            api_key=os.environ.get("LTENGINE_API_KEY", ""),
            llm_api_key=os.environ.get("LTENGINE_LLM_API_KEY", ""),
            llm_model=os.environ.get("LTENGINE_LLM_MODEL", ""),
            verbose=os.environ.get("LTENGINE_VERBOSE", "") == "1",
        )
