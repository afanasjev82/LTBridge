from __future__ import annotations

import os
import platform
import socket
import subprocess
import sys
import threading
import time
from dataclasses import dataclass
from pathlib import Path

import httpx
import pytest
import uvicorn

from .mock_upstream import MockBehavior, MockState, create_mock_app

REPO_ROOT = Path(__file__).resolve().parents[2]
EXE = "ltengine.exe" if platform.system() == "Windows" else "ltengine"
RUST_BIN = Path(os.environ.get("LTENGINE_REF_BIN",
                REPO_ROOT / ".rust-ref" / "target" / "release" / EXE))


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


class MockControl:
    def __init__(self, base_url: str) -> None:
        self.base_url = base_url

    def set(self, behavior: MockBehavior) -> None:
        httpx.post(f"{self.base_url}/__mock__/behavior", json=behavior.__dict__).raise_for_status()

    def reset(self) -> None:
        httpx.post(f"{self.base_url}/__mock__/reset").raise_for_status()

    def requests(self) -> list[dict]:
        r = httpx.get(f"{self.base_url}/__mock__/requests")
        r.raise_for_status()
        return r.json()


def start_mock() -> tuple[MockControl, uvicorn.Server, threading.Thread]:
    port = free_port()
    state = MockState()
    config = uvicorn.Config(create_mock_app(state), host="127.0.0.1", port=port,
                            log_level="warning", lifespan="off")
    server = uvicorn.Server(config)
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    ctl = MockControl(f"http://127.0.0.1:{port}")
    _wait_http(f"{ctl.base_url}/v1/models")
    return ctl, server, thread


def _wait_http(url: str, timeout: float = 30.0) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            if httpx.get(url, timeout=2.0).status_code < 500:
                return
        except httpx.HTTPError:
            time.sleep(0.15)
    raise RuntimeError(f"server at {url} did not come up within {timeout}s")


@dataclass
class ServerHandle:
    base_url: str
    mock: MockControl
    proc: subprocess.Popen


@dataclass
class ServerPair:
    rust: ServerHandle
    py: ServerHandle

    def reset(self) -> None:
        self.rust.mock.reset()
        self.py.mock.reset()


def _launch_rust(mock_url: str, extra_flags: list[str]) -> ServerHandle:
    if not RUST_BIN.exists():
        pytest.skip(f"Rust reference missing ({RUST_BIN}); run scripts/build_rust_ref.py")
    port = free_port()
    proc = subprocess.Popen(
        [str(RUST_BIN), "--llm-url", mock_url, "--host", "127.0.0.1",
         "--port", str(port), *extra_flags],
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    base = f"http://127.0.0.1:{port}"
    _wait_http(f"{base}/languages")
    return ServerHandle(base, None, proc)  # mock attached by caller


# Python server launch command (verified working, Task 3):
#   <sys.executable> -m ltbridge --llm-url <mock_url> --host 127.0.0.1 --port <port> ...
#
# `python -m ltbridge` works because this repo is a single uv workspace (backend + frontend
# share ONE venv at the repo root, confirmed: no backend/.venv exists) with `ltbridge`
# installed editable into it (`.venv/Lib/site-packages/_editable_impl_ltbridge.pth`) and a
# `[project.scripts] ltbridge = ...` console entry point registered
# (`.venv/Scripts/ltbridge.exe`). `__main__.py` has an `if __name__ == "__main__": main()`
# guard, so `python -m ltbridge` dispatches into the same `main()` the console script calls.
# As long as pytest itself is launched via `uv run` (from repo root or with
# `--project backend` — both resolve to the same shared venv here), `sys.executable` inside
# the pytest process already IS that venv's interpreter, so plain `sys.executable -m ltbridge`
# needs no `uv run` re-wrapping and stays fast to start. Verified directly:
#   .venv/Scripts/python.exe -m ltbridge --help   (from repo root AND from backend/) -> works.
def _launch_py(mock_url: str, extra_flags: list[str]) -> ServerHandle:
    port = free_port()
    proc = subprocess.Popen(
        [sys.executable, "-m", "ltbridge", "--llm-url", mock_url, "--host", "127.0.0.1",
         "--port", str(port), *extra_flags],
        cwd=str(REPO_ROOT / "backend"),
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    base = f"http://127.0.0.1:{port}"
    _wait_http(f"{base}/languages")
    return ServerHandle(base, None, proc)


@pytest.fixture(scope="module")
def make_pair(request):
    """Factory: launch a Rust+Python pair, each with its own mock. Module-scoped."""
    created: list[ServerPair] = []

    def _make(extra_flags: list[str] | None = None) -> ServerPair:
        flags = extra_flags or []
        rust_mock, _, _ = start_mock()
        py_mock, _, _ = start_mock()
        rust = _launch_rust(rust_mock.base_url, flags)
        rust.mock = rust_mock
        py = _launch_py(py_mock.base_url, flags)
        py.mock = py_mock
        pair = ServerPair(rust, py)
        created.append(pair)
        return pair

    yield _make
    for pair in created:
        for h in (pair.rust, pair.py):
            h.proc.terminate()
            try:
                h.proc.wait(timeout=10)
            except subprocess.TimeoutExpired:
                h.proc.kill()


@pytest.fixture(scope="module")
def pair(make_pair) -> ServerPair:
    """Default-flag server pair (char-limit 5000, no api key, no token caps)."""
    return make_pair([])
