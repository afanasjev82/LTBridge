"""Live sanity tests against a real vLLM upstream.

Excluded by default (``addopts = "-m 'not live'"`` in pyproject.toml, marker
registered there too). Run explicitly:

    uv run --project backend pytest tests/live -m live -v

LLM URL resolution matches ``bench/bench_overhead.py``: override via the
``LTENGINE_BENCH_LLM_URL`` env var when ``http://localhost:8000`` isn't
reachable from inside WSL on this host (see bench/REPORT.md for what was
verified here).
"""

from __future__ import annotations

import os

import httpx
import pytest

from tests.differential.conftest import _launch_py, _launch_rust

LIVE_URL = os.environ.get("LTENGINE_BENCH_LLM_URL", "http://localhost:8000")


@pytest.mark.live
def test_live_translate_both_servers() -> None:
    rust = _launch_rust(LIVE_URL, [])
    py = _launch_py(LIVE_URL, [])
    try:
        for srv in (rust, py):
            r = httpx.post(
                f"{srv.base_url}/translate",
                json={"q": "Привет, мир", "source": "auto", "target": "en"},
                timeout=120.0,
            )
            assert r.status_code == 200
            body = r.json()
            assert body["translatedText"].strip()
            assert body["detectedLanguage"]["language"] == "ru"
    finally:
        for s in (rust, py):
            s.proc.terminate()


@pytest.mark.live
def test_live_long_text_heartbeat_py() -> None:
    py = _launch_py(LIVE_URL, [])
    try:
        long_q = "The quick brown fox jumps over the lazy dog. " * 80  # ~3600 chars
        r = httpx.post(
            f"{py.base_url}/translate",
            json={"q": long_q, "source": "en", "target": "ru"},
            timeout=300.0,
        )
        assert r.status_code == 200
        assert r.json()["translatedText"].strip()
    finally:
        py.proc.terminate()
