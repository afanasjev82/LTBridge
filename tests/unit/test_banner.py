"""Unit coverage for server/banner.py's startup banner renderer.

Console-output only: `render_banner` is a pure function of `(Config,
resolved_model, workers) -> str`, so this file never touches the HTTP
surface — that boundary is guarded by the differential suite instead (see
tests/differential/). The last test here (`test_...exits_1`) is the one
process-level check: it launches `python -m ltengine` for real, against a
dead port with no --llm-model, to prove the pre-resolve fail-fast path.
"""

from __future__ import annotations

import re
import socket
import subprocess
import sys
from pathlib import Path

import pytest
from ltengine.config import Config
from ltengine.server.banner import render_banner

REPO_ROOT = Path(__file__).resolve().parents[2]


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def _make_config(**overrides: object) -> Config:
    defaults: dict[str, object] = {
        "llm_url": "http://127.0.0.1:9999",
        "host": "0.0.0.0",
        "port": 5050,
        "char_limit": 5000,
        "api_key": "",
        "llm_api_key": "",
        "llm_model": "",
        "llm_max_tokens": 0,
        "llm_chars_per_token": 0.0,
        "llm_max_tokens_mult": 0.0,
        "llm_max_tokens_floor": 64,
        "llm_timeout": 120.0,
        "verbose": False,
    }
    defaults.update(overrides)
    return Config(**defaults)  # type: ignore[arg-type]


# ---------------------------------------------------------------------------
# Art + version line
# ---------------------------------------------------------------------------


def test_render_banner_contains_ascii_art_and_subtitle() -> None:
    text = render_banner(_make_config(), resolved_model="demo-model", workers=1)
    assert "Local AI Machine Translation" in text
    # A distinctive, exact line from the Rust reference's ASCII art (banner.rs)
    # — proves the art was copied verbatim, not paraphrased.
    assert "/_____/_/ /_____/_/ /_/\\__, /_/_/ /_/\\___/" in text


def test_render_banner_version_line_shape() -> None:
    text = render_banner(_make_config(), resolved_model="demo-model", workers=1)
    assert re.search(r"LTEngine-py \S+ · api · python \d+\.\d+\.\d+", text)


# ---------------------------------------------------------------------------
# Runtime rows
# ---------------------------------------------------------------------------


def test_render_banner_runtime_rows_reflect_config() -> None:
    config = _make_config(
        llm_url="http://10.0.0.5:8000",
        host="127.0.0.1",
        port=6060,
        char_limit=999,
        llm_timeout=30.0,
    )
    text = render_banner(config, resolved_model="demo-model", workers=4)

    assert re.search(r"mode\s+api", text)
    assert "http://10.0.0.5:8000" in text
    assert "demo-model (resolved)" in text
    assert re.search(r"bind\s+127\.0\.0\.1:6060", text)
    assert re.search(r"char limit\s+999", text)
    assert re.search(r"llm timeout\s+30s", text)
    assert re.search(r"workers\s+4", text)


def test_render_banner_llm_timeout_zero_reads_none() -> None:
    config = _make_config(llm_timeout=0.0)
    text = render_banner(config, resolved_model="m", workers=1)
    assert re.search(r"llm timeout\s+none", text)


def test_render_banner_explicit_model_has_no_resolved_suffix() -> None:
    config = _make_config(llm_model="explicit-model")
    text = render_banner(config, resolved_model=None, workers=1)
    assert "explicit-model" in text
    assert "(resolved)" not in text


# ---------------------------------------------------------------------------
# Secrets: masked, never echoed
# ---------------------------------------------------------------------------


def test_render_banner_masks_secret_values() -> None:
    config = _make_config(api_key="server-secret-value", llm_api_key="llm-secret-value")
    text = render_banner(config, resolved_model="m", workers=1)

    assert "server-secret-value" not in text
    assert "llm-secret-value" not in text
    assert re.search(r"llm api key\s+set", text)
    assert re.search(r"server auth\s+set", text)


def test_render_banner_reports_none_for_empty_secrets() -> None:
    config = _make_config(api_key="", llm_api_key="")
    text = render_banner(config, resolved_model="m", workers=1)

    assert re.search(r"llm api key\s+none", text)
    assert re.search(r"server auth\s+none", text)


# ---------------------------------------------------------------------------
# Token budget section
# ---------------------------------------------------------------------------


def test_token_budget_disabled_when_knobs_unset() -> None:
    config = _make_config(llm_max_tokens=0, llm_chars_per_token=0.0, llm_max_tokens_mult=0.0)
    text = render_banner(config, resolved_model="m", workers=1)
    assert "Token budget  DISABLED" in text
    assert "Token budget  ENABLED" not in text


def test_token_budget_disabled_when_only_some_knobs_set() -> None:
    # mult and chars_per_token set, but no ceiling (--llm-max-tokens 0) -> still disabled.
    config = _make_config(llm_max_tokens=0, llm_chars_per_token=4.0, llm_max_tokens_mult=1.5)
    text = render_banner(config, resolved_model="m", workers=1)
    assert "Token budget  DISABLED" in text


def test_token_budget_enabled_when_all_knobs_set() -> None:
    config = _make_config(
        llm_max_tokens=4096,
        llm_chars_per_token=4.0,
        llm_max_tokens_mult=1.5,
        llm_max_tokens_floor=64,
    )
    text = render_banner(config, resolved_model="m", workers=1)

    assert "Token budget  ENABLED" in text
    assert re.search(r"chars/token\s+4", text)
    assert re.search(r"mult\s+×1\.5", text)
    assert re.search(r"floor\s+64", text)
    assert re.search(r"ceiling\s+4096", text)


# ---------------------------------------------------------------------------
# Pre-resolve fail-fast (process-level)
# ---------------------------------------------------------------------------


def test_missing_llm_model_pre_resolve_failure_exits_1() -> None:
    """No --llm-model + an unreachable --llm-url must exit 1 with a clear
    stderr message, BEFORE Granian ever starts (fail-fast startup, matching
    the Rust reference's `process::exit(1)` on a failed `resolve_model()`)."""
    dead_port = _free_port()  # bound-then-closed: guaranteed connection refused
    proc = subprocess.run(
        [
            sys.executable,
            "-m",
            "ltengine",
            "--llm-url",
            f"http://127.0.0.1:{dead_port}",
            "--host",
            "127.0.0.1",
            "--port",
            str(_free_port()),
        ],
        cwd=str(REPO_ROOT / "backend"),
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )
    assert proc.returncode == 1, f"stdout={proc.stdout!r} stderr={proc.stderr!r}"
    assert "Failed to resolve model" in proc.stderr
    # The banner (which needs a resolved model) must never have printed.
    assert "LTEngine-py" not in proc.stdout


@pytest.mark.parametrize("workers", [1, 3])
def test_render_banner_workers_row_is_python_specific(workers: int) -> None:
    text = render_banner(_make_config(llm_model="m"), resolved_model=None, workers=workers)
    assert re.search(rf"workers\s+{workers}\b", text)
