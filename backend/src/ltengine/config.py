"""Server configuration dataclass."""

from __future__ import annotations

from dataclasses import dataclass, field


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
