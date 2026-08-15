"""Live sanity tests against a real vLLM upstream.

Excluded by default (``addopts = "-m 'not live'"`` in pyproject.toml, marker
registered there too). Run explicitly:

    uv run --project backend pytest tests/live -m live -v

LLM URL resolution matches ``bench/bench_overhead.py``: override via the
``LTBRIDGE_BENCH_LLM_URL`` env var when ``http://localhost:8000`` isn't
reachable from inside WSL on this host (see bench/REPORT.md for what was
verified here).
"""

from __future__ import annotations

import os
import subprocess

import httpx
import pytest

from tests.differential.conftest import _launch_py, _launch_rust

LIVE_URL = os.environ.get("LTBRIDGE_BENCH_LLM_URL", "http://localhost:8000")


def _terminate(proc: subprocess.Popen) -> None:
    """Same teardown as `tests/differential/conftest.py`'s `make_pair` fixture:
    terminate, then wait up to 10s, then kill if it didn't exit — a bare
    `.terminate()` with no wait/kill fallback can leave an orphaned process behind
    if the server doesn't unwind cleanly within some bounded time.
    """
    proc.terminate()
    try:
        proc.wait(timeout=10)
    except subprocess.TimeoutExpired:
        proc.kill()


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
        _terminate(rust.proc)
        _terminate(py.proc)


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
        _terminate(py.proc)
