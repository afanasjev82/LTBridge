# LTEngine-py Strict-Parity Port — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make `LTEngine-py` a byte-parity replacement for Rust LTEngine api-mode, proven by a zero-waiver differential suite and benchmarked against the Rust binary (Medusa V2 · P1 gate).

**Architecture:** A differential harness (recording mock upstream + side-by-side Rust/Python server runners) is built first; every parity gap from the audit becomes a red test, fixed in the scaffold until green with zero waivers. Then a suite-protected pure-structure refactor into `core/` + `server/`, then benchmarks.

**Tech Stack:** Python 3.12, uv workspace, Litestar, httpx, uvicorn (mock + dev server), pytest + pytest-asyncio, prometheus-client, maturin/PyO3 (whatlang binding), Rust reference: `LTEngine@development` built `--no-default-features --features api`.

## Global Constraints

- **Parity bar:** strict byte-parity, ZERO waivers (spec §2). Rust is the oracle — when the plan says "transcribe observed Rust output", the observed value is the contract, not an invention.
- **Never touch the user's `W:\Work\Projects\smartech\LTEngine` working tree branch** (it sits on `main`). The Rust reference is built ONLY in the dedicated worktree `.rust-ref/` pinned to `development` (dev HEAD reference: `3070771`).
- **Commits:** Conventional Commits (`type(scope): summary`; scopes here: `harness`, `core`, `server`, `detect`, `bench`, `docs`, `chore`). **No Co-Authored-By lines — ever.** One parity fix per commit. Restructure commits are pure moves — no behavior edits mixed in.
- **Branch:** all work on `development` in `W:\Work\Projects\smartech\LTEngine-py`.
- **Quality gates:** `uv run ruff check .` and `uv run mypy backend` clean before every commit that touches `backend/`.
- **Windows dev box:** all commands below run from `W:\Work\Projects\smartech\LTEngine-py` unless stated; use forward-slash paths in Python code; the Rust binary is `ltengine.exe` on Windows.
- **Live vLLM (Layer 3 / B2 only):** `http://localhost:8000` (`smartech-ltengine-vllm`, model `gemma-4-12b-it`). Tests hitting it carry `@pytest.mark.live` and are excluded by default.
- **Prompt/quirk reference:** the approved spec `docs/superpowers/specs/2026-08-11-ltengine-py-strict-parity-port-design.md` §5 is the authoritative fix list; Rust sources live in `.rust-ref/ltengine/src/` after Task 2.

---

### Task 1: Test tooling + prompt/budget unit seeds

**Files:**
- Modify: `backend/pyproject.toml` (dev deps + pytest/ruff/mypy config)
- Create: `tests/__init__.py` (empty), `tests/unit/__init__.py` (empty)
- Create: `tests/unit/test_prompt.py`
- Test: `tests/unit/test_prompt.py`

**Interfaces:**
- Consumes: `ltengine.prompt.PromptBuilder` (exists in scaffold).
- Produces: pytest runnable via `uv run pytest`; markers `live` registered; later tasks add tests under `tests/`.

- [ ] **Step 1: Add dev dependencies and tool config**

In `backend/pyproject.toml`, add (merging with existing content — inspect first with `cat backend/pyproject.toml`):

```toml
[dependency-groups]
dev = [
  "pytest>=8.0",
  "pytest-asyncio>=0.24",
  "httpx>=0.27",
  "uvicorn>=0.30",
  "prometheus-client>=0.20",
  "ruff>=0.6",
  "mypy>=1.11",
]

[tool.pytest.ini_options]
testpaths = ["../tests"]
asyncio_mode = "auto"
markers = ["live: hits the real vLLM container; excluded by default"]
addopts = "-m 'not live'"

[tool.ruff]
line-length = 100

[tool.mypy]
python_version = "3.12"
strict = false
check_untyped_defs = true
```

Also inspect `backend/src/ltengine/__main__.py` now and note the CLI arg names it parses (needed by Task 5); record them in the task notes.

Run: `uv sync` — expected: resolves and installs dev group.

- [ ] **Step 2: Write failing prompt-parity unit tests**

`tests/unit/test_prompt.py` — the expected strings are transcribed verbatim from Rust `prompt.rs:37-59` (available in `.rust-ref` after Task 2; they are also pinned in spec §3.4 of the V2 program spec and match the scaffold):

```python
from ltengine.prompt import PromptBuilder

SYSTEM_TEXT = (
    "You are an expert linguist, specializing in translation. "
    "You are able to capture the nuances of the languages you translate. "
    "You pay attention to masculine/feminine/plural and proper use of articles and grammar. "
    "You always provide natural sounding translations that fully preserve the meaning of the original text. "
    "You never provide explanations for your work. "
    "You always answer with the translated text and nothing else."
)


def test_explicit_source_user_prompt() -> None:
    p = PromptBuilder().set_source_language("English").set_target_language("Russian").set_format("text").build("Hello")
    assert p.system == SYSTEM_TEXT
    assert p.user == "Translate the text below from English to Russian.\n\nEnglish: Hello\n\nRussian:\n"


def test_auto_source_user_prompt() -> None:
    p = PromptBuilder().set_source_language("auto").set_target_language("German").build("Hi")
    assert p.user == "Translate the text below to German.\n\nText: Hi\n\nGerman:\n"


def test_html_system_prompt_has_tag_sentence() -> None:
    p = PromptBuilder().set_format("html").set_target_language("French").build("<b>x</b>")
    assert "You must preserve all HTML tags and elements in the translation. " in p.system
    assert p.system.endswith("You always answer with the translated text and nothing else.")
```

- [ ] **Step 3: Run tests**

Run: `uv run pytest ../tests/unit/test_prompt.py -v` (from `backend/`) or `uv run --project backend pytest tests/unit/test_prompt.py -v` from repo root.
Expected: PASS (scaffold prompts are already verbatim). If any fails, the scaffold prompt text drifted — fix `prompt.py` to the strings above.

- [ ] **Step 4: Commit**

```bash
git add backend/pyproject.toml tests/ uv.lock
git commit -m "chore(harness): pytest/ruff/mypy tooling + prompt parity unit seeds"
```

---

### Task 2: Rust reference worktree + build script

**Files:**
- Create: `scripts/build_rust_ref.py`
- Modify: `.gitignore` (add `.rust-ref/`)

**Interfaces:**
- Produces: `scripts/build_rust_ref.py` (idempotent; exit 0 with binary path printed). Binary path convention consumed by Task 3's conftest: env `LTENGINE_REF_BIN`, else `.rust-ref/target/release/ltengine(.exe)`.

- [ ] **Step 1: Write the build script**

`scripts/build_rust_ref.py`:

```python
"""Build the Rust LTEngine api-mode reference binary in a dedicated worktree.

Never touches the main LTEngine working tree's checked-out branch.
Idempotent: safe to re-run; prints the binary path on success.
"""
from __future__ import annotations

import platform
import subprocess
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
LTENGINE_REPO = Path("W:/Work/Projects/smartech/LTEngine")
REF_DIR = REPO_ROOT / ".rust-ref"
REF_BRANCH = "development"


def run(cmd: list[str], cwd: Path | None = None) -> None:
    print("+", " ".join(cmd))
    subprocess.run(cmd, cwd=cwd, check=True)


def main() -> int:
    if not LTENGINE_REPO.exists():
        print(f"ERROR: Rust repo not found at {LTENGINE_REPO}", file=sys.stderr)
        return 1
    if not REF_DIR.exists():
        run(["git", "-C", str(LTENGINE_REPO), "worktree", "add",
             str(REF_DIR), REF_BRANCH])
    else:
        run(["git", "-C", str(REF_DIR), "checkout", REF_BRANCH])
        run(["git", "-C", str(REF_DIR), "pull", "--ff-only"])
    head = subprocess.run(["git", "-C", str(REF_DIR), "rev-parse", "--short", "HEAD"],
                          capture_output=True, text=True, check=True).stdout.strip()
    print(f"reference: LTEngine@{REF_BRANCH} {head}")
    run(["cargo", "build", "--release", "--no-default-features", "--features", "api",
         "--manifest-path", str(REF_DIR / "ltengine" / "Cargo.toml"),
         "--target-dir", str(REF_DIR / "target")])
    exe = "ltengine.exe" if platform.system() == "Windows" else "ltengine"
    binary = REF_DIR / "target" / "release" / exe
    if not binary.exists():
        print(f"ERROR: expected binary missing: {binary}", file=sys.stderr)
        return 1
    print(f"BINARY: {binary}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
```

- [ ] **Step 2: Add `.rust-ref/` to `.gitignore`** (append the line `.rust-ref/`).

- [ ] **Step 3: Run it**

Run: `uv run python scripts/build_rust_ref.py`
Expected: worktree created at `.rust-ref/`, cargo builds (first build takes minutes), final line `BINARY: ...ltengine.exe`. Verify: `.rust-ref/target/release/ltengine.exe --help` prints CLI help including `--llm-url`.

- [ ] **Step 4: Commit**

```bash
git add scripts/build_rust_ref.py .gitignore
git commit -m "feat(harness): rust reference worktree build script (.rust-ref, api features only)"
```

---

### Task 3: Recording mock upstream + dual-server fixtures + smoke parity

**Files:**
- Create: `tests/differential/__init__.py` (empty)
- Create: `tests/differential/mock_upstream.py`
- Create: `tests/differential/conftest.py`
- Create: `tests/differential/helpers.py`
- Create: `tests/differential/test_smoke_parity.py`

**Interfaces:**
- Produces (consumed by ALL later differential tasks):
  - `mock_upstream.MockBehavior(reply_text: str = "MOCK_TRANSLATION", status: int = 200, delay_s: float = 0.0, body_mode: str = "ok", custom_body: str = "")` — `body_mode ∈ {"ok","garbage","empty_choices","custom"}`.
  - conftest fixture `pair` (module-scoped factory result): `ServerPair(rust: ServerHandle, py: ServerHandle)`; each `ServerHandle` has `.base_url: str`, `.mock: MockControl`.
  - `MockControl.set(behavior: MockBehavior) -> None`, `.reset() -> None`, `.requests() -> list[dict]` (recorded chat-completions payloads, JSON-decoded).
  - `helpers.fire_both(pair, method: str, path: str, *, json_body=None, form=None, files=None, headers=None) -> tuple[Snapshot, Snapshot]`
  - `helpers.Snapshot(status: int, content_type: str, body: bytes, chunked: bool, leading_spaces: int, json: Any | None)`
  - `helpers.assert_parity(rust: Snapshot, py: Snapshot, *, normalize_heartbeat: bool = False) -> None`
  - `helpers.launch_pair_factory` — module fixture creator taking extra CLI flags: `make_pair(extra_flags: list[str]) -> ServerPair` (each server gets ITS OWN mock instance).

- [ ] **Step 1: Write the mock upstream**

`tests/differential/mock_upstream.py`:

```python
"""Scriptable, recording mock of an OpenAI-compatible upstream (raw ASGI)."""
from __future__ import annotations

import asyncio
import json
from dataclasses import dataclass, field
from typing import Any


@dataclass
class MockBehavior:
    reply_text: str = "MOCK_TRANSLATION"
    status: int = 200
    delay_s: float = 0.0
    body_mode: str = "ok"  # ok | garbage | empty_choices | custom
    custom_body: str = ""


@dataclass
class MockState:
    behavior: MockBehavior = field(default_factory=MockBehavior)
    requests: list[dict[str, Any]] = field(default_factory=list)


def _ok_body(text: str) -> bytes:
    return json.dumps({
        "id": "cmpl-mock", "object": "chat.completion", "model": "mock-model",
        "choices": [{"index": 0, "finish_reason": "stop",
                     "message": {"role": "assistant", "content": text}}],
    }).encode()


MODELS_BODY = json.dumps({
    "object": "list",
    "data": [{"id": "mock-model", "object": "model"}],
}).encode()


def create_mock_app(state: MockState):
    async def send_json(send, status: int, body: bytes) -> None:
        await send({"type": "http.response.start", "status": status,
                    "headers": [(b"content-type", b"application/json")]})
        await send({"type": "http.response.body", "body": body})

    async def read_body(receive) -> bytes:
        chunks: list[bytes] = []
        while True:
            msg = await receive()
            if msg["type"] == "http.request":
                chunks.append(msg.get("body", b""))
                if not msg.get("more_body"):
                    break
            elif msg["type"] == "http.disconnect":
                break
        return b"".join(chunks)

    async def app(scope, receive, send) -> None:
        if scope["type"] != "http":
            return
        path, method = scope["path"], scope["method"]
        if path == "/v1/models" and method == "GET":
            await send_json(send, 200, MODELS_BODY)
        elif path == "/v1/chat/completions" and method == "POST":
            raw = await read_body(receive)
            state.requests.append(json.loads(raw))
            b = state.behavior
            if b.delay_s:
                await asyncio.sleep(b.delay_s)
            if b.body_mode == "garbage":
                await send_json(send, b.status, b"this is not json {{{")
            elif b.body_mode == "empty_choices":
                await send_json(send, b.status, json.dumps({"choices": []}).encode())
            elif b.body_mode == "custom":
                await send_json(send, b.status, b.custom_body.encode())
            else:
                await send_json(send, b.status, _ok_body(b.reply_text))
        elif path == "/__mock__/behavior" and method == "POST":
            state.behavior = MockBehavior(**json.loads(await read_body(receive)))
            await send_json(send, 200, b"{}")
        elif path == "/__mock__/reset" and method == "POST":
            state.behavior = MockBehavior()
            state.requests.clear()
            await send_json(send, 200, b"{}")
        elif path == "/__mock__/requests" and method == "GET":
            await send_json(send, 200, json.dumps(state.requests).encode())
        else:
            await send_json(send, 404, b'{"error":"mock: unknown path"}')

    return app
```

- [ ] **Step 2: Write conftest (ports, subprocess servers, mock control)**

`tests/differential/conftest.py`:

```python
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


def _launch_py(mock_url: str, extra_flags: list[str]) -> ServerHandle:
    port = free_port()
    proc = subprocess.Popen(
        [sys.executable, "-m", "ltengine", "--llm-url", mock_url, "--host", "127.0.0.1",
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
```

Note: `python -m ltengine` must work; if the scaffold only exposes a console-script, add a `backend/src/ltengine/__main__.py` guard (`from .cli import main; main()` shape already exists — verify from Task 1's inspection notes and adjust the launch command to whatever actually starts the server; record the final form in this conftest).

- [ ] **Step 3: Write helpers**

`tests/differential/helpers.py`:

```python
from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any

import httpx

from .conftest import ServerPair


@dataclass
class Snapshot:
    status: int
    content_type: str
    body: bytes
    chunked: bool
    leading_spaces: int
    json: Any | None


def _snap(resp: httpx.Response) -> Snapshot:
    body = resp.content
    stripped = body.lstrip(b" ")
    leading = len(body) - len(stripped)
    try:
        parsed = json.loads(stripped) if stripped else None
    except json.JSONDecodeError:
        parsed = None
    return Snapshot(
        status=resp.status_code,
        content_type=resp.headers.get("content-type", "").split(";")[0].strip(),
        body=body,
        chunked=resp.headers.get("transfer-encoding", "") == "chunked",
        leading_spaces=leading,
        json=parsed,
    )


def _fire(base_url: str, method: str, path: str, *, json_body=None, form=None,
          files=None, headers=None, timeout: float = 60.0) -> Snapshot:
    with httpx.Client(timeout=timeout) as c:
        resp = c.request(method, f"{base_url}{path}", json=json_body, data=form,
                         files=files, headers=headers)
    return _snap(resp)


def fire_both(pair: ServerPair, method: str, path: str, **kw) -> tuple[Snapshot, Snapshot]:
    return (_fire(pair.rust.base_url, method, path, **kw),
            _fire(pair.py.base_url, method, path, **kw))


def assert_parity(rust: Snapshot, py: Snapshot, *, normalize_heartbeat: bool = False) -> None:
    assert py.status == rust.status, f"status: py={py.status} rust={rust.status}"
    assert py.content_type == rust.content_type, (
        f"content-type: py={py.content_type!r} rust={rust.content_type!r}")
    if normalize_heartbeat:
        # Heartbeat space count is timing-jittery; require both chunked with >=1 space,
        # then compare the JSON payloads exactly.
        assert rust.chunked and py.chunked, "expected chunked responses on both"
        assert rust.leading_spaces >= 1 and py.leading_spaces >= 1
        assert py.json == rust.json, f"json: py={py.json} rust={rust.json}"
    else:
        assert py.body == rust.body, f"body: py={py.body!r} rust={rust.body!r}"


def last_payload(handle) -> dict:
    reqs = handle.mock.requests()
    assert reqs, "no chat-completions request recorded"
    return reqs[-1]
```

- [ ] **Step 4: Smoke parity test**

`tests/differential/test_smoke_parity.py`:

```python
from .helpers import assert_parity, fire_both


def test_languages_parity(pair) -> None:
    rust, py = fire_both(pair, "GET", "/languages")
    assert rust.status == 200
    assert_parity(rust, py)


def test_translate_happy_path_parity(pair) -> None:
    pair.reset()
    rust, py = fire_both(pair, "POST", "/translate",
                         json_body={"q": "Hello", "source": "en", "target": "ru"})
    assert rust.status == 200
    assert_parity(rust, py)
```

- [ ] **Step 5: Run the smoke tests**

Run: `uv run --project backend pytest tests/differential/test_smoke_parity.py -v`
Expected: `test_languages_parity` PASS. `test_translate_happy_path_parity` likely PASS at the body level (both return `{"translatedText":"MOCK_TRANSLATION"}`). Debug harness issues (ports, startup) until both run deterministically. If `/languages` itself diverges (ordering/serialization), fix `app.py` serialization to match Rust bytes — that is a real parity finding, not a harness bug.

- [ ] **Step 6: Commit**

```bash
git add tests/differential/
git commit -m "feat(harness): recording mock upstream, dual-server fixtures, smoke parity"
```

---

### Task 4: Payload parity — `enable_thinking` invariant

**Files:**
- Create: `tests/differential/test_payload_parity.py`
- Modify: `backend/src/ltengine/llm_client.py`

**Interfaces:**
- Consumes: `pair`, `fire_both`, `last_payload` from Task 3.
- Produces: `LLMClient.translate(system: str, user: str, max_tokens: int | None = None) -> str` (the `max_tokens` param lands in Task 5; add the parameter now with default `None`, always omitted when `None`).

- [ ] **Step 1: Write the failing test**

`tests/differential/test_payload_parity.py`:

```python
from .helpers import fire_both, last_payload


def test_chat_payload_identical(pair) -> None:
    pair.reset()
    fire_both(pair, "POST", "/translate",
              json_body={"q": "Hello", "source": "en", "target": "ru"})
    rust_payload = last_payload(pair.rust)
    py_payload = last_payload(pair.py)
    assert py_payload == rust_payload
```

- [ ] **Step 2: Run to verify it fails**

Run: `uv run --project backend pytest tests/differential/test_payload_parity.py -v`
Expected: FAIL — the Rust payload contains `"chat_template_kwargs": {"enable_thinking": false}` (and possibly `max_tokens`, depending on default flags: with no `--llm-max-tokens` flag the Rust default is 0 ⇒ omitted, so `enable_thinking` should be the only diff). Print both payloads in the failure message to record the exact Rust shape.

- [ ] **Step 3: Fix the client payload**

In `llm_client.py`, change `translate` to:

```python
    async def translate(self, system: str, user: str, max_tokens: int | None = None) -> str:
        endpoint = f"{self.url}/v1/chat/completions"
        payload: dict[str, object] = {
            "model": self.model,
            "messages": [
                {"role": "system", "content": system},
                {"role": "user", "content": user},
            ],
            "temperature": 0.0,
            "chat_template_kwargs": {"enable_thinking": False},
        }
        if max_tokens is not None:
            payload["max_tokens"] = max_tokens
        ...
```

Keep key insertion order matching the observed Rust payload if the differential compare is dict-based (it is — `==` on parsed JSON ignores order; do NOT byte-compare payloads).

- [ ] **Step 4: Run to verify it passes**

Run: `uv run --project backend pytest tests/differential/test_payload_parity.py -v`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add tests/differential/test_payload_parity.py backend/src/ltengine/llm_client.py
git commit -m "fix(core): send enable_thinking:false chat_template_kwargs (parity)"
```

---

### Task 5: Token budget pipeline + CLI flags + `--llm-timeout`

**Files:**
- Create: `backend/src/ltengine/token_budget.py`
- Create: `tests/unit/test_token_budget.py`
- Create: `tests/differential/test_budget_parity.py`
- Modify: `backend/src/ltengine/config.py`, `backend/src/ltengine/__main__.py` (CLI args), `backend/src/ltengine/app.py` (wire cap into translate), `backend/src/ltengine/llm_client.py` (timeout)

**Interfaces:**
- Produces:
  - `token_budget.output_cap(input_chars: int, llm_max_tokens: int, chars_per_token: float, mult: float, floor: int) -> int | None` — `None` means "omit max_tokens".
  - `Config` gains: `llm_max_tokens: int = 0`, `llm_chars_per_token: float = 0.0`, `llm_max_tokens_mult: float = 0.0`, `llm_max_tokens_floor: int = 64`, `llm_timeout: float = 120.0`.
  - CLI flags: `--llm-max-tokens`, `--llm-chars-per-token`, `--llm-max-tokens-mult`, `--llm-max-tokens-floor`, `--llm-timeout` (names identical to Rust; verify against `.rust-ref/target/release/ltengine.exe --help` and match exactly).

- [ ] **Step 1: Write failing unit tests**

`tests/unit/test_token_budget.py`:

```python
from ltengine.token_budget import output_cap


def test_disabled_when_max_tokens_zero() -> None:
    assert output_cap(1000, 0, 3.0, 1.5, 64) is None


def test_static_ceiling_when_dynamic_knobs_unset() -> None:
    assert output_cap(1000, 16384, 0.0, 0.0, 64) == 16384


def test_dynamic_cap() -> None:
    # ceil(1000/3)=334; ceil(334*1.5)=501; max(64)->501; min(16384)->501
    assert output_cap(1000, 16384, 3.0, 1.5, 64) == 501


def test_floor_applies() -> None:
    # ceil(30/3)=10; ceil(10*1.5)=15; max(64)->64
    assert output_cap(30, 16384, 3.0, 1.5, 64) == 64


def test_ceiling_wins_over_floor() -> None:
    # huge input -> dynamic 50001 -> min(500) = 500 even though floor is 64
    assert output_cap(100_000, 500, 3.0, 1.5, 64) == 500
```

- [ ] **Step 2: Run to verify failure**

Run: `uv run --project backend pytest tests/unit/test_token_budget.py -v`
Expected: FAIL with `ModuleNotFoundError: ltengine.token_budget`.

- [ ] **Step 3: Implement `token_budget.py`**

```python
"""Output token budget — port of LTEngine's token_budget.rs + output_cap()."""
from __future__ import annotations

import math


def output_cap(input_chars: int, llm_max_tokens: int, chars_per_token: float,
               mult: float, floor: int) -> int | None:
    """Return the max_tokens value to send, or None to omit the field.

    Mirrors Rust: disabled entirely when llm_max_tokens == 0; dynamic budget only
    when chars_per_token AND mult are > 0; cap = ceil(ceil(chars/cpt) * mult),
    then raised to floor, then clamped to llm_max_tokens (ceiling wins over floor).
    """
    if llm_max_tokens <= 0:
        return None
    if chars_per_token > 0 and mult > 0:
        cap = math.ceil(math.ceil(input_chars / chars_per_token) * mult)
        cap = max(cap, floor)
        return min(cap, llm_max_tokens)
    return llm_max_tokens
```

- [ ] **Step 4: Run unit tests** — expected: PASS.

- [ ] **Step 5: Wire config, CLI, app, client timeout**

- `config.py`: add the five fields with the defaults from Interfaces above.
- `__main__.py`: add the five `argparse` options (exact flag spellings from Rust `--help`); pass into `Config`.
- `app.py` translate handler: compute `cap = output_cap(len(q), config.llm_max_tokens, config.llm_chars_per_token, config.llm_max_tokens_mult, config.llm_max_tokens_floor)` (note: `len(q)` — Rust uses `chars().count()` for the cap, characters not bytes) and pass `max_tokens=cap` to `llm_client.translate(...)`.
- `llm_client.py` / app lifespan: build the shared `httpx.AsyncClient` with `timeout=None if config.llm_timeout == 0 else httpx.Timeout(config.llm_timeout)`.

- [ ] **Step 6: Differential budget test**

`tests/differential/test_budget_parity.py`:

```python
from .helpers import fire_both, last_payload

FLAGS = ["--llm-max-tokens", "16384", "--llm-chars-per-token", "3.0",
         "--llm-max-tokens-mult", "1.5", "--llm-max-tokens-floor", "64"]


def test_max_tokens_value_parity(make_pair) -> None:
    pair = make_pair(FLAGS)
    pair.reset()
    fire_both(pair, "POST", "/translate",
              json_body={"q": "x" * 1000, "source": "en", "target": "ru"})
    assert last_payload(pair.py) == last_payload(pair.rust)  # includes max_tokens=501


def test_max_tokens_absent_by_default(pair) -> None:
    pair.reset()
    fire_both(pair, "POST", "/translate",
              json_body={"q": "Hello", "source": "en", "target": "ru"})
    assert "max_tokens" not in last_payload(pair.py)
    assert "max_tokens" not in last_payload(pair.rust)
```

Run: `uv run --project backend pytest tests/differential/test_budget_parity.py tests/unit -v`
Expected: PASS. If the Rust payload's `max_tokens` differs from 501, the Rust formula transcription is wrong — re-read `.rust-ref/ltengine/src/token_budget.rs` and fix `token_budget.py` AND the unit-test vectors to match observed Rust behavior (Rust is the oracle).

- [ ] **Step 7: Commit**

```bash
git add backend/src/ltengine/ tests/
git commit -m "feat(core): max_tokens budget pipeline + llm-timeout flags (parity)"
```

---

### Task 6: Error taxonomy + global `{"error": ...}` handlers

**Files:**
- Create: `backend/src/ltengine/errors.py`
- Create: `scripts/dump_rust_responses.py`
- Create: `tests/differential/test_error_parity.py`
- Modify: `backend/src/ltengine/app.py`, `backend/src/ltengine/llm_client.py`

**Interfaces:**
- Produces:
  - `errors.LlmError(Exception)` with `.http_status: int` and `.message: str`.
  - `errors.map_llm_failure(exc_or_status: Exception | tuple[int, str]) -> LlmError` — transient (connect error, timeout, upstream 502/503/504/429) → 503; other upstream statuses (message embeds upstream body verbatim), JSON-parse failure, empty choices → 500.
  - `llm_client.translate` raises `LlmError` only.
  - App-level exception handlers: every non-2xx response body is `{"error": "<message>"}`; 404 for unknown paths; message strings match observed Rust output byte-for-byte.

- [ ] **Step 1: Write the Rust-response dump helper** (used to transcribe exact strings)

`scripts/dump_rust_responses.py`:

```python
"""Fire error-provoking cases at a manually-started Rust server; print raw responses.

Usage: uv run python scripts/dump_rust_responses.py http://127.0.0.1:PORT MOCK_CTL_URL
Start the pair by hand first (see tests/differential/conftest.py), or run via pytest -s
with a debugger breakpoint; simplest: python -i with the conftest imported.
"""
from __future__ import annotations

import sys

import httpx

CASES: list[tuple[str, dict]] = [
    ("upstream_502", {"status": 502, "body_mode": "ok"}),
    ("upstream_503", {"status": 503, "body_mode": "ok"}),
    ("upstream_504", {"status": 504, "body_mode": "ok"}),
    ("upstream_429", {"status": 429, "body_mode": "ok"}),
    ("upstream_400_with_body", {"status": 400, "body_mode": "custom",
                                "custom_body": '{"detail":"boom"}'}),
    ("garbage_json", {"status": 200, "body_mode": "garbage"}),
    ("empty_choices", {"status": 200, "body_mode": "empty_choices"}),
]


def main() -> None:
    server, mock = sys.argv[1], sys.argv[2]
    for name, behavior in CASES:
        httpx.post(f"{mock}/__mock__/behavior",
                   json={"reply_text": "MOCK_TRANSLATION", "delay_s": 0.0, **behavior})
        r = httpx.post(f"{server}/translate",
                       json={"q": "Hello", "source": "en", "target": "ru"}, timeout=30.0)
        print(f"--- {name}: HTTP {r.status_code}\n{r.text!r}\n")


if __name__ == "__main__":
    main()
```

- [ ] **Step 2: Capture the exact Rust error strings**

Start a Rust server + mock manually (reuse conftest interactively):

```bash
uv run --project backend python - <<'EOF'
import tests.differential.conftest as c
mock, _, _ = c.start_mock()
rust = c._launch_rust(mock.base_url, [])
rust.mock = mock
print("RUST:", rust.base_url, "MOCK:", mock.base_url)
input("press enter to stop")
EOF
```

In a second terminal: `uv run python scripts/dump_rust_responses.py <RUST_URL> <MOCK_URL>`.
Transcribe every printed status + body verbatim into the test table in Step 3 (replace the `RECORD_ME` sentinels). Also capture: connect-refused (point a Rust server at a dead port) and read-timeout (`--llm-timeout 2` + mock delay 5 — note the heartbeat makes slow failures a 200; the timeout case at 2s exceeds the 1s grace, so expect the chunked-200 wart here, NOT a 503: record what Rust actually does).

- [ ] **Step 3: Write the failing differential error tests**

`tests/differential/test_error_parity.py`:

```python
import pytest

from .helpers import assert_parity, fire_both
from .mock_upstream import MockBehavior

BODY = {"q": "Hello", "source": "en", "target": "ru"}

# (name, behavior). Expected statuses/bodies are NOT hardcoded — parity with
# Rust is the assertion. The dump script (Task 6 Step 2) documents observed values
# in bench/notes for humans; the tests only require rust == py.
ERROR_CASES = [
    ("upstream_502", MockBehavior(status=502)),
    ("upstream_503", MockBehavior(status=503)),
    ("upstream_504", MockBehavior(status=504)),
    ("upstream_429", MockBehavior(status=429)),
    ("upstream_400_with_body", MockBehavior(status=400, body_mode="custom",
                                            custom_body='{"detail":"boom"}')),
    ("garbage_json", MockBehavior(body_mode="garbage")),
    ("empty_choices", MockBehavior(body_mode="empty_choices")),
]


@pytest.mark.parametrize("name,behavior", ERROR_CASES, ids=[c[0] for c in ERROR_CASES])
def test_llm_failure_parity(pair, name, behavior) -> None:
    pair.reset()
    pair.rust.mock.set(behavior)
    pair.py.mock.set(behavior)
    rust, py = fire_both(pair, "POST", "/translate", json_body=BODY)
    assert rust.status in (500, 503), f"unexpected rust status {rust.status} — re-check case"
    assert_parity(rust, py)


def test_unknown_path_parity(pair) -> None:
    rust, py = fire_both(pair, "GET", "/definitely-not-a-real-path-xyz")
    assert_parity(rust, py)


def test_malformed_json_parity(pair) -> None:
    rust, py = fire_both(pair, "POST", "/translate",
                         headers={"content-type": "application/json"},
                         # httpx `content=` for raw body:
                         )
    # fire_both lacks raw-body support: extend helpers._fire with `content=` kwarg
    # (add `content=None` parameter passed through to c.request) as part of this task.
```

Complete `test_malformed_json_parity` by adding a `content: bytes | None = None` passthrough kwarg to `helpers._fire`/`fire_both`, then:

```python
def test_malformed_json_parity(pair) -> None:
    rust, py = fire_both(pair, "POST", "/translate",
                         headers={"content-type": "application/json"},
                         content=b"{not json")
    assert_parity(rust, py)
```

- [ ] **Step 4: Run to verify failures**

Run: `uv run --project backend pytest tests/differential/test_error_parity.py -v`
Expected: FAIL widely — the scaffold returns 200-with-original-text on LLM failures, Litestar emits `{"detail":...}`/`{"status_code":...}` shapes for malformed JSON and its own 404 for unknown paths.

- [ ] **Step 5: Implement `errors.py` + client mapping + app handlers**

`backend/src/ltengine/errors.py` (message templates: use the strings transcribed in Step 2 — the `f"..."` bodies below show the shape; replace with observed text exactly):

```python
"""LLM failure taxonomy — port of llm_api.rs ApiError + error_response.rs."""
from __future__ import annotations

TRANSIENT_UPSTREAM = {429, 502, 503, 504}


class LlmError(Exception):
    def __init__(self, http_status: int, message: str) -> None:
        super().__init__(message)
        self.http_status = http_status
        self.message = message


def unreachable(detail: str) -> LlmError:
    return LlmError(503, f"LLM API unreachable: {detail}")     # ← replace with observed Rust text


def timeout(detail: str) -> LlmError:
    return LlmError(503, f"LLM API timed out: {detail}")       # ← replace with observed Rust text


def remote_status(status: int, body: str) -> LlmError:
    http = 503 if status in TRANSIENT_UPSTREAM else 500
    return LlmError(http, f"LLM API returned status {status}: {body}")


def bad_response(detail: str) -> LlmError:
    return LlmError(500, f"Invalid LLM API response: {detail}")  # ← replace with observed Rust text


def empty_choices() -> LlmError:
    return LlmError(500, "LLM API returned no choices")          # ← replace with observed Rust text
```

`llm_client.translate`: wrap the request in `try/except httpx.ConnectError/ConnectTimeout → unreachable`, `httpx.TimeoutException → timeout`; non-2xx → `remote_status(status, resp.text)`; JSON decode failure → `bad_response`; empty choices → `empty_choices()`.

`app.py`: remove the silent catch (`except Exception: raw = q`); instead let `LlmError` propagate to a Litestar exception handler returning `Response({"error": e.message}, status_code=e.http_status)`. Add handlers for `404` (`{"error": ...}` matching observed Rust body for unknown paths — likely actix's default empty 404: match whatever the dump shows), `405`, and validation/malformed-JSON errors (match the observed Rust serde message shape). Register via `Litestar(exception_handlers={...})`.

- [ ] **Step 6: Run to verify green**

Run: `uv run --project backend pytest tests/differential/test_error_parity.py tests/differential/test_smoke_parity.py -v`
Expected: PASS all. Iterate on exact message strings until byte-equal.

- [ ] **Step 7: Commit**

```bash
git add backend/src/ltengine/ tests/differential/ scripts/dump_rust_responses.py
git commit -m "fix(server): Rust error taxonomy + global {\"error\"} handlers (kills silent-200)"
```

---

### Task 7: Request-validation parity batch

**Files:**
- Create: `tests/differential/test_validation_parity.py`
- Modify: `backend/src/ltengine/app.py`

**Interfaces:**
- Consumes: Task 3 fixtures/helpers (incl. `make_pair` for the api-key module).
- Produces: validation behavior relied on by all later tasks; no new public interfaces.

- [ ] **Step 1: Write the failing tests** (one module; api-key cases get their own pair)

`tests/differential/test_validation_parity.py`:

```python
import pytest

from .helpers import assert_parity, fire_both

B = {"q": "Hello", "source": "en", "target": "ru"}


CASES = [
    ("missing_q", "POST", "/translate", {"json_body": {"source": "en", "target": "ru"}}),
    ("blank_q", "POST", "/translate", {"json_body": {**B, "q": "   "}}),
    ("missing_source", "POST", "/translate", {"json_body": {"q": "x", "target": "ru"}}),
    ("missing_target", "POST", "/translate", {"json_body": {"q": "x", "source": "en"}}),
    ("bad_format", "POST", "/translate", {"json_body": {**B, "format": "pdf"}}),
    ("unknown_source", "POST", "/translate", {"json_body": {**B, "source": "xx"}}),
    ("unknown_target", "POST", "/translate", {"json_body": {**B, "target": "xx"}}),
    ("alias_source", "POST", "/translate", {"json_body": {**B, "source": "zh-Hans", "target": "en"}}),
    ("form_urlencoded", "POST", "/translate", {"form": B}),
    ("multipart", "POST", "/translate", {"files": {k: (None, v) for k, v in B.items()}}),
    ("unsupported_content_type", "POST", "/translate",
     {"headers": {"content-type": "text/plain"}, "content": b"q=x"}),
    ("alternatives_zero", "POST", "/translate", {"json_body": {**B, "alternatives": 0}}),
    ("alternatives_two", "POST", "/translate", {"json_body": {**B, "alternatives": 2}}),
    ("alternatives_garbage", "POST", "/translate", {"json_body": {**B, "alternatives": "lots"}}),
    ("identity_translation", "POST", "/translate",
     {"json_body": {"q": "  Hello  ", "source": "en", "target": "en"}}),
    ("byte_limit_multibyte", "POST", "/translate",
     {"json_body": {"q": "я" * 2600, "source": "ru", "target": "en"}}),  # 5200 bytes > 5000
    ("detect_ok", "POST", "/detect", {"json_body": {"q": "Hello world"}}),
    ("detect_missing_q", "POST", "/detect", {"json_body": {}}),
    ("detect_over_limit", "POST", "/detect", {"json_body": {"q": "я" * 2600}}),
    ("frontend_settings", "GET", "/frontend/settings", {}),
    ("translate_file_501", "POST", "/translate_file", {"json_body": {}}),
    ("suggest_501", "POST", "/suggest", {"json_body": {}}),
]


@pytest.mark.parametrize("name,method,path,kw", CASES, ids=[c[0] for c in CASES])
def test_validation_parity(pair, name, method, path, kw) -> None:
    pair.reset()
    rust, py = fire_both(pair, method, path, **kw)
    assert_parity(rust, py)


AUTH_FLAGS = ["--api-key", "sekret"]


@pytest.mark.parametrize("case", ["translate_no_key", "translate_wrong_key",
                                  "translate_right_key", "detect_no_key",
                                  "settings_still_lies"])
def test_api_key_parity(make_pair, case) -> None:
    pair = make_pair(AUTH_FLAGS)
    pair.reset()
    body = {"q": "Hello", "source": "en", "target": "ru"}
    if case == "translate_no_key":
        rust, py = fire_both(pair, "POST", "/translate", json_body=body)
    elif case == "translate_wrong_key":
        rust, py = fire_both(pair, "POST", "/translate", json_body={**body, "api_key": "nope"})
    elif case == "translate_right_key":
        rust, py = fire_both(pair, "POST", "/translate", json_body={**body, "api_key": "sekret"})
    elif case == "detect_no_key":
        rust, py = fire_both(pair, "POST", "/detect", json_body={"q": "Hello"})
    else:  # settings_still_lies: Rust hardcodes keyRequired/apiKeys false even with a key set
        rust, py = fire_both(pair, "GET", "/frontend/settings")
    assert_parity(rust, py)
```

Note: `detect_ok` compares detection results — until Task 9 (whatlang) this may FAIL on confidence values. If it does, mark ONLY the detection-dependent cases with `@pytest.mark.xfail(reason="whatlang parity lands in Task 9", strict=True)` — `detect_ok`, and any `/translate` case with `source: "auto"` — and remove the xfail in Task 9. Everything else must go green in THIS task.

- [ ] **Step 2: Run to verify failures**

Run: `uv run --project backend pytest tests/differential/test_validation_parity.py -v`
Expected failures at minimum: `unsupported_content_type` (no gate), `alternatives_garbage` (500 vs 400), `identity_translation` (missing trim), `byte_limit_multibyte` (char count vs bytes), `detect_over_limit` (no limit check on /detect), `settings_still_lies` (`keyRequired: true` vs Rust `false`), `detect_no_key` (missing key check).

- [ ] **Step 3: Fix `app.py`**

Apply, one logical block at a time, re-running the failing case after each:
- Content-type gate before parsing: JSON / `application/x-www-form-urlencoded` / `multipart/form-data` only; else 400 `{"error": "Unsupported content-type"}` (verify exact text against Rust output from the test failure diff).
- Char limit: `len(q.encode("utf-8")) > config.char_limit` for `/translate` AND `/detect`; error message must interpolate the BYTE length (match Rust bytes).
- `/detect`: add api-key check (403, same body as translate) and the char-limit check; ordering must match Rust `check_params` (missing-param → api-key → limit — confirm order from Rust failures if a combined case diverges).
- `/frontend/settings`: hardcode `"apiKeys": False, "keyRequired": False`.
- Identity path: `translated_text = _improve_formatting(q, q)` when `source == target`.
- `alternatives`: parse with `try: int(...) except (TypeError, ValueError): return 400` with the Rust-matching message (transcribe from the test diff).

- [ ] **Step 4: Run the full differential suite**

Run: `uv run --project backend pytest tests/differential -v`
Expected: all green except documented xfails (detection cases). Fix any straggler until zero non-xfail failures.

- [ ] **Step 5: Commit**

```bash
git add backend/src/ltengine/app.py tests/differential/test_validation_parity.py
git commit -m "fix(server): validation parity batch — content-type gate, byte limit, detect checks, quirks"
```

---

### Task 8: Grace-race + heartbeat streaming + disconnect cancellation

**Files:**
- Create: `backend/src/ltengine/streaming.py`
- Create: `tests/differential/test_streaming_parity.py`
- Create: `tests/unit/test_streaming_cancel.py`
- Modify: `backend/src/ltengine/app.py`

**Interfaces:**
- Produces: `streaming.respond_with_grace(work: Coroutine[Any, Any, dict], fallback_body: dict) -> Response | Stream` — awaits `work` up to `GRACE_PERIOD_S = 1.0`; fast completion → normal `Response` (success or `LlmError` propagates); slow → chunked 200 emitting one b" " per `HEARTBEAT_INTERVAL_S = 1.0`, then the success body, or `fallback_body` if `work` ultimately fails (the Rust wart); cancels `work` if the client disconnects.

- [ ] **Step 1: Write the failing differential tests**

`tests/differential/test_streaming_parity.py`:

```python
from .helpers import assert_parity, fire_both
from .mock_upstream import MockBehavior

B = {"q": "Hello", "source": "en", "target": "ru"}


def test_slow_success_heartbeat(pair) -> None:
    pair.reset()
    for h in (pair.rust, pair.py):
        h.mock.set(MockBehavior(delay_s=3.0))
    rust, py = fire_both(pair, "POST", "/translate", json_body=B, timeout=30.0)
    assert rust.status == 200 and rust.chunked and rust.leading_spaces >= 1
    assert_parity(rust, py, normalize_heartbeat=True)


def test_slow_failure_returns_200_original_text(pair) -> None:
    """The bug-for-bug wart: >1s then failure => chunked 200 with original q."""
    pair.reset()
    for h in (pair.rust, pair.py):
        h.mock.set(MockBehavior(delay_s=2.0, status=502))
    rust, py = fire_both(pair, "POST", "/translate", json_body=B, timeout=30.0)
    assert rust.status == 200
    assert rust.json == {"translatedText": "Hello"}
    assert_parity(rust, py, normalize_heartbeat=True)


def test_fast_path_stays_buffered(pair) -> None:
    pair.reset()
    rust, py = fire_both(pair, "POST", "/translate", json_body=B)
    assert not rust.chunked and rust.leading_spaces == 0
    assert_parity(rust, py)
```

- [ ] **Step 2: Run to verify failures**

Run: `uv run --project backend pytest tests/differential/test_streaming_parity.py -v`
Expected: `test_slow_success_heartbeat` FAIL (Python not chunked, no spaces); `test_slow_failure_returns_200_original_text` FAIL (Python now 503s after Task 6 — correct for fast failures, but the >1s path must revert to the wart); fast path PASS.

- [ ] **Step 3: Implement `streaming.py`**

```python
"""Grace-race + heartbeat streaming — port of main.rs GRACE_PERIOD/HEARTBEAT logic."""
from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncGenerator, Coroutine
from typing import Any

from litestar.response import Response, Stream

GRACE_PERIOD_S = 1.0
HEARTBEAT_INTERVAL_S = 1.0


async def respond_with_grace(work: Coroutine[Any, Any, dict],
                             fallback_body: dict) -> Response | Stream:
    task: asyncio.Task[dict] = asyncio.create_task(work)
    done, _ = await asyncio.wait({task}, timeout=GRACE_PERIOD_S)
    if done:
        # Fast path: buffered response; success or LlmError propagates to handlers.
        return Response(content=task.result(), media_type="application/json")

    async def stream() -> AsyncGenerator[bytes, None]:
        try:
            while True:
                d, _ = await asyncio.wait({task}, timeout=HEARTBEAT_INTERVAL_S)
                if d:
                    break
                yield b" "
            try:
                body = task.result()
            except Exception:
                body = fallback_body  # the Rust wart: slow failure => original text, HTTP 200
            yield json.dumps(body).encode()
        finally:
            if not task.done():
                task.cancel()  # client disconnected mid-stream: abort upstream call

    return Stream(stream(), status_code=200, media_type="application/json")
```

Wire in `app.py`: the translate handler builds `async def work() -> dict` (LLM call + `improve_formatting` + result-dict assembly incl. `detectedLanguage`/`alternatives`) and returns `await respond_with_grace(work(), fallback_body=assemble_result(q))` where `assemble_result(q)` is the same result-dict builder run on the original text. JSON serialization of the streamed body must match Litestar's buffered serialization byte-for-byte (`json.dumps` separators: use `json.dumps(body, separators=(",", ":"))` if the buffered path emits compact JSON — check against the fast-path body and match).

- [ ] **Step 4: Run to verify green**

Run: `uv run --project backend pytest tests/differential/test_streaming_parity.py tests/differential/test_error_parity.py -v`
Expected: streaming tests PASS; error tests still PASS (fast failures — mock replies instantly — remain proper 5xx; only >1s failures take the wart path). If an error case regressed, the grace-race must apply to ALL translate LLM calls with failure timing deciding the shape — exactly like Rust.

- [ ] **Step 5: Unit test for disconnect cancellation**

`tests/unit/test_streaming_cancel.py`:

```python
import asyncio

import pytest

from ltengine.streaming import respond_with_grace


@pytest.mark.asyncio
async def test_disconnect_cancels_work() -> None:
    started = asyncio.Event()
    cancelled = asyncio.Event()

    async def work() -> dict:
        started.set()
        try:
            await asyncio.sleep(60)
        except asyncio.CancelledError:
            cancelled.set()
            raise
        return {"translatedText": "never"}

    resp = await respond_with_grace(work(), fallback_body={"translatedText": "orig"})
    gen = resp.content  # the async generator behind the Stream
    assert await gen.__anext__() == b" "        # first heartbeat
    await gen.aclose()                          # simulates client disconnect
    await asyncio.wait_for(cancelled.wait(), timeout=2)
```

Run: `uv run --project backend pytest tests/unit/test_streaming_cancel.py -v` — expected: PASS (the `finally: task.cancel()` fires on `aclose()`).

- [ ] **Step 6: Commit**

```bash
git add backend/src/ltengine/streaming.py backend/src/ltengine/app.py tests/
git commit -m "feat(server): grace-race heartbeat streaming + disconnect cancellation (parity incl. slow-failure wart)"
```

---

### Task 9: whatlang-parity detection

**Files:**
- Create: `whatlang-py/Cargo.toml`, `whatlang-py/pyproject.toml`, `whatlang-py/src/lib.rs`
- Create: `tests/differential/test_detect_parity.py`
- Modify: `backend/src/ltengine/languages.py` (replace lingua), `backend/pyproject.toml` (swap dependency), root `pyproject.toml` (workspace member if needed)

**Interfaces:**
- Produces: Python module `whatlang_py` with `detect(text: str) -> tuple[str, int] | None` returning `(english_name_after_mandarin_remap, confidence_0_100_truncated)`; `ltengine.languages.detect_language(text)` keeps its existing signature (`DetectionResult(language, confidence)`), now backed by `whatlang_py`.

- [ ] **Step 1: Check PyPI for an existing binding**

Run: `uv pip install whatlang-pyo3 --dry-run` and `uv pip install whatlang --dry-run`.
If a maintained binding exists AND pins whatlang 0.18 semantics, evaluate it against Step 4's fixtures before writing the vendored crate. Expected: none suitable → vendor (Steps 2–3). If one passes Step 4 unmodified, use it and skip to Step 5.

- [ ] **Step 2: Write the vendored PyO3 crate**

`whatlang-py/Cargo.toml`:

```toml
[package]
name = "whatlang-py"
version = "0.1.0"
edition = "2021"

[lib]
name = "whatlang_py"
crate-type = ["cdylib"]

[dependencies]
pyo3 = { version = "0.22", features = ["extension-module"] }
whatlang = "0.18"
```

`whatlang-py/pyproject.toml`:

```toml
[build-system]
requires = ["maturin>=1.5"]
build-backend = "maturin"

[project]
name = "whatlang-py"
version = "0.1.0"
requires-python = ">=3.12"
```

`whatlang-py/src/lib.rs` — replicates languages.rs detection semantics (allowlist over supported names, Mandarin→Chinese remap, truncated integer confidence):

```rust
use pyo3::prelude::*;
use whatlang::{Detector, Lang};

/// English display names of the 49 supported languages (post-remap), as in languages.rs.
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

fn remap(name: &str) -> &str {
    if name == "Mandarin" { "Chinese" } else { name }
}

fn allowlist() -> Vec<Lang> {
    Lang::all()
        .iter()
        .copied()
        .filter(|l| SUPPORTED.contains(&remap(l.eng_name())))
        .collect()
}

/// Returns (english_name_after_remap, confidence 0-100 truncated) or None.
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
```

IMPORTANT: before finalizing, open `.rust-ref/ltengine/src/languages.rs` and compare its detector construction line-by-line (allowlist source, remap, confidence math) — adjust `lib.rs` to be a faithful copy of THAT logic, not this sketch, if they differ.

- [ ] **Step 3: Build and wire into the workspace**

```bash
uv add --project backend "whatlang-py @ file:///W:/Work/Projects/smartech/LTEngine-py/whatlang-py"
uv sync
```

(uv builds it via maturin; requires the Rust toolchain already present from Task 2.) Verify: `uv run --project backend python -c "import whatlang_py; print(whatlang_py.detect('Привет, мир'))"` → `('Russian', <int>)`.

- [ ] **Step 4: Replace lingua in `languages.py` and write the failing differential test**

In `languages.py`: delete the lingua import, `_LINGUA_TO_INTERNAL`, `_SUPPORTED_LINGUA`, `_detector`; add a name→internal map and reimplement:

```python
import whatlang_py

_NAME_TO_INTERNAL: dict[str, str] = {name: code for code, _alias, name in _LANG_DEFS}


def detect_language(text: str) -> DetectionResult:
    """Detect via whatlang (parity with languages.rs). English/conf 0 on any failure."""
    fallback = DetectionResult(language=LANGUAGES[0], confidence=0)
    result = whatlang_py.detect(text)
    if result is None:
        return fallback
    name, confidence = result
    internal = _NAME_TO_INTERNAL.get(name)
    if internal is None:
        return fallback
    lang = _BY_INTERNAL.get(internal)
    if lang is None:
        return fallback
    return DetectionResult(language=lang, confidence=confidence)
```

Remove `lingua` from `backend/pyproject.toml` dependencies.

`tests/differential/test_detect_parity.py`:

```python
import pytest

from .helpers import assert_parity, fire_both

TEXTS = [
    "Hello, how are you today?", "Привет, как дела?", "Bonjour tout le monde",
    "Hallo, wie geht es dir?", "Hola, ¿cómo estás?", "Ciao, come stai?",
    "こんにちは、お元気ですか", "안녕하세요", "你好，世界", "مرحبا بالعالم",
    "Tere, kuidas läheb?", "Cześć, jak się masz?", "Merhaba dünya",
    "Xin chào thế giới", "สวัสดีครับ", "Γειά σου κόσμε", "שלום עולם",
    "Hej, hur mår du?", "Ahoj, jak se máš?", "Salut, ce mai faci?",
    "ok", "123", "a", "", " ",  # short/ambiguous/degenerate
]


@pytest.mark.parametrize("text", TEXTS, ids=[t[:12] or "empty" for t in TEXTS])
def test_detect_parity(pair, text) -> None:
    rust, py = fire_both(pair, "POST", "/detect", json_body={"q": text})
    assert_parity(rust, py)


def test_translate_auto_detected_language_parity(pair) -> None:
    pair.reset()
    rust, py = fire_both(pair, "POST", "/translate",
                         json_body={"q": "Привет, мир", "source": "auto", "target": "en"})
    assert_parity(rust, py)
```

- [ ] **Step 5: Run, fix divergences, remove Task 7 xfails**

Run: `uv run --project backend pytest tests/differential/test_detect_parity.py -v`
Expected: initially some FAIL if lib.rs drifted from languages.rs — fix lib.rs (Step 2 IMPORTANT note) until every case matches. Then delete the `xfail` markers added in Task 7 and run the whole suite:
`uv run --project backend pytest tests/differential tests/unit -v` — expected: all green, zero xfails.
Note: degenerate cases ("", " ") go through the missing-q validation (400) — they pin that path's parity too.

- [ ] **Step 6: Commit**

```bash
git add whatlang-py/ backend/ tests/ uv.lock
git commit -m "feat(detect): whatlang-parity detection via vendored PyO3 binding, lingua removed"
```

---

### Task 10: `/metrics` endpoint

**Files:**
- Create: `tests/unit/test_metrics_endpoint.py`
- Modify: `backend/src/ltengine/app.py`

**Interfaces:**
- Produces: `GET /metrics` → prometheus text format, metric names prefixed `ltengine_` (e.g. `ltengine_requests_total{method,path,status}`, `ltengine_request_duration_seconds`). Presence + content-type + prefix tested; values exempt from parity (spec §2).

- [ ] **Step 1: Failing test**

`tests/unit/test_metrics_endpoint.py`:

```python
from litestar.testing import TestClient

from ltengine.app import create_app
from ltengine.config import Config


def test_metrics_endpoint_present() -> None:
    app = create_app(Config(llm_url="http://127.0.0.1:1", llm_model="m"))
    with TestClient(app=app) as client:
        client.get("/languages")
        r = client.get("/metrics")
        assert r.status_code == 200
        assert r.headers["content-type"].startswith("text/plain")
        assert "ltengine_requests_total" in r.text
```

Run: expected FAIL (404 / index.html fallback).

- [ ] **Step 2: Implement** — add a `prometheus_client` `CollectorRegistry` in `create_app`; a middleware/after-hook incrementing `ltengine_requests_total` (labels `method`, `path`, `status`) and observing `ltengine_request_duration_seconds` (label `method`); a `/metrics` handler returning `generate_latest(registry)` with `CONTENT_TYPE_LATEST`. Add `/metrics` to the static-middleware API-prefix list so it is never shadowed.

- [ ] **Step 3: Run** — expected PASS; also run full suite (`uv run --project backend pytest tests -v`) to confirm no differential regressions (the Rust server also exposes `/metrics`; no differential test compares them).

- [ ] **Step 4: Commit**

```bash
git add backend/src/ltengine/app.py tests/unit/test_metrics_endpoint.py
git commit -m "feat(server): prometheus /metrics with ltengine_ prefix"
```

---

### Task 11: 404 static-path semantics

**Files:**
- Create: `tests/differential/test_static_parity.py`
- Modify: `backend/src/ltengine/app.py` (StaticFilesMiddleware)

- [ ] **Step 1: Failing tests**

`tests/differential/test_static_parity.py`:

```python
from .helpers import assert_parity, fire_both


def test_unknown_path_404_parity(pair) -> None:
    rust, py = fire_both(pair, "GET", "/no/such/thing.txt")
    assert_parity(rust, py)


def test_root_serves_html_status(pair) -> None:
    # Bodies differ by design (different UIs) — compare status + content-type only.
    rust, py = fire_both(pair, "GET", "/")
    assert py.status == rust.status == 200
    assert py.content_type == rust.content_type == "text/html"
```

- [ ] **Step 2: Run** — expected: `test_unknown_path_404_parity` FAIL (Python serves index.html 200; Rust 404s — confirm from the diff).

- [ ] **Step 3: Fix the middleware** — in `StaticFilesMiddleware`, serve `index.html` ONLY for `/`; for other non-API paths serve the file if it exists, else fall through to the app's 404 handler (which Task 6 made emit the Rust-matching 404 body). Delete the `candidate if candidate.exists() else index.html` fallback.

- [ ] **Step 4: Run** — both tests + full suite green.

- [ ] **Step 5: Commit**

```bash
git add backend/src/ltengine/app.py tests/differential/test_static_parity.py
git commit -m "fix(server): 404 for unknown paths — index.html only at root (parity)"
```

---

### Task 12: Protected restructure into `core/` + `server/`

**Files:**
- Create: `backend/src/ltengine/core/__init__.py`, `backend/src/ltengine/server/__init__.py`, `backend/src/ltengine/server/routes.py`
- Move (git mv): `prompt.py`→`core/prompt.py`, `languages.py`→`core/languages.py`, `token_budget.py`→`core/token_budget.py`, `errors.py`→`core/errors.py`; `streaming.py`→`server/streaming.py`, `app.py`→`server/app.py`; extract `_improve_formatting` into `core/formatting.py` as `improve_formatting`
- Modify: `backend/src/ltengine/__init__.py`, `__main__.py`, all moved-module imports; `tests/*` imports

**Interfaces:**
- Produces (the P2/P3 mount contract):
  - `ltengine.core` — importable WITHOUT Litestar installed (no server imports anywhere under `core/`).
  - `ltengine.server.routes.create_router(config: Config, llm_client: LLMClient) -> litestar.Router` — all API route handlers, no static middleware (the standalone app adds that).
  - `ltengine.server.app.create_app(config: Config) -> Litestar` — unchanged public signature.
  - Re-exports for compatibility: `ltengine.core.formatting.improve_formatting(source: str, translation: str) -> str`.

- [ ] **Step 1: Confirm the full suite is green** (`uv run --project backend pytest tests -v`) — this is the safety net; do not restructure on red.

- [ ] **Step 2: Pure-move commit 1 (core/)** — `git mv` the four core modules + extract `improve_formatting` into `core/formatting.py` (cut-paste, zero edits beyond the `def` rename to drop the underscore); fix imports mechanically. Run full suite → green.

```bash
git add -A
git commit -m "refactor(core): move transport-free modules into ltengine.core (pure move)"
```

- [ ] **Step 3: Pure-move commit 2 (server/)** — create `server/routes.py` housing every route handler in a `create_router(config, llm_client)` factory returning `litestar.Router(path="/", route_handlers=[...])`; `server/app.py` keeps `create_app` = lifespan + `create_router` + StaticFilesMiddleware + exception handlers + `/metrics`. Handlers move verbatim (closure DI unchanged). Run full suite → green.

```bash
git add -A
git commit -m "refactor(server): route handlers behind create_router factory (pure move)"
```

- [ ] **Step 4: Core isolation guard** — add `tests/unit/test_core_isolation.py`:

```python
import subprocess
import sys


def test_core_imports_without_litestar() -> None:
    code = (
        "import sys;"
        "sys.modules['litestar'] = None;"  # poison: any litestar import will fail loudly
        "import ltengine.core.prompt, ltengine.core.languages,"
        " ltengine.core.token_budget, ltengine.core.errors, ltengine.core.formatting;"
        "print('ok')"
    )
    out = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True)
    assert out.returncode == 0 and "ok" in out.stdout, out.stderr
```

Run → PASS. Commit:

```bash
git add tests/unit/test_core_isolation.py
git commit -m "test(core): guard — core package imports without litestar"
```

---

### Task 13: B1 wrapper-overhead benchmark + B2 e2e sanity + report

**Files:**
- Create: `bench/bench_overhead.py`, `bench/texts.py`, `bench/REPORT.md` (generated content committed)
- Create: `tests/live/__init__.py` (empty), `tests/live/test_live_sanity.py`

**Interfaces:**
- Consumes: conftest launch machinery (imported from `tests.differential.conftest`).
- Produces: `bench/REPORT.md` with B1 + B2 tables; exit code 0 = thresholds met.

- [ ] **Step 1: Write the bencher**

`bench/texts.py`:

```python
"""20 real short texts for B2 (varied languages/lengths). Salt per run to defeat prefix caching."""
import uuid

TEXTS = [
    "The quick brown fox jumps over the lazy dog.",
    "Der schnelle braune Fuchs springt über den faulen Hund.",
    "Le renard brun rapide saute par-dessus le chien paresseux.",
    "El rápido zorro marrón salta sobre el perro perezoso.",
    "Быстрая коричневая лиса перепрыгивает через ленивую собаку.",
    "Il volpe marrone salta velocemente sopra il cane pigro.",
    "De snelle bruine vos springt over de luie hond.",
    "A raposa marrom rápida pula sobre o cachorro preguiçoso.",
    "Szybki brązowy lis przeskakuje nad leniwym psem.",
    "Rävens snabba språng över den lata hunden förvånade alla.",
    "Kiire pruun rebane hüppab üle laisa koera.",
    "Nopea ruskea kettu hyppää laiskan koiran yli.",
    "Rychlá hnědá liška skáče přes líného psa.",
    "Gyors barna róka ugrik át a lusta kutyán.",
    "Vulpea maro rapidă sare peste câinele leneș.",
    "Hızlı kahverengi tilki tembel köpeğin üzerinden atlar.",
    "Швидка коричнева лисиця перестрибує через ледачого пса.",
    "Brzi smeđi lisac skače preko lijenog psa.",
    "Ang mabilis na kayumangging soro ay tumalon sa tamad na aso.",
    "Con cáo nâu nhanh nhẹn nhảy qua con chó lười biếng.",
]


def salted(text: str) -> str:
    return f"{text} [{uuid.uuid4().hex[:8]}]"
```

`bench/bench_overhead.py`:

```python
"""B1: wrapper overhead vs the Rust reference on a zero-delay mock upstream.

Usage:
  uv run --project backend python bench/bench_overhead.py            # B1 (mock)
  uv run --project backend python bench/bench_overhead.py --b2 URL  # B2 (real vLLM at URL)

Thresholds (spec §7): py p50 overhead <= rust p50 overhead + 3ms; py RPS >= 70% rust RPS at C=32.
"""
from __future__ import annotations

import argparse
import asyncio
import statistics
import sys
import time
from pathlib import Path

import httpx

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from tests.differential import conftest as h  # noqa: E402
from bench.texts import TEXTS, salted  # noqa: E402

CONCURRENCIES = [1, 8, 32]
N_PER_ARM = 2000
BODY = {"q": "Hello, world", "source": "en", "target": "ru"}


async def run_arm(url: str, n: int, concurrency: int, bodies=None) -> dict:
    latencies: list[float] = []
    sem = asyncio.Semaphore(concurrency)
    async with httpx.AsyncClient(timeout=120.0) as client:
        async def one(i: int) -> None:
            body = bodies[i % len(bodies)] if bodies else BODY
            async with sem:
                t0 = time.perf_counter()
                r = await client.post(f"{url}/translate", json=body)
                r.raise_for_status()
                latencies.append(time.perf_counter() - t0)
        t0 = time.perf_counter()
        await asyncio.gather(*(one(i) for i in range(n)))
        wall = time.perf_counter() - t0
    latencies.sort()
    q = statistics.quantiles(latencies, n=100)
    return {"p50_ms": q[49] * 1000, "p95_ms": q[94] * 1000,
            "p99_ms": q[98] * 1000, "rps": n / wall}


async def bench_pair(rust_url: str, py_url: str, mock_url: str, n: int) -> list[str]:
    lines = ["| C | arm | p50 ms | p95 ms | p99 ms | RPS |", "|---|---|---|---|---|---|"]
    results: dict[tuple[int, str], dict] = {}
    for c in CONCURRENCIES:
        for name, url in [("direct-mock", mock_url), ("rust", rust_url), ("py", py_url)]:
            if name == "direct-mock":
                # measure the mock floor via raw chat-completions calls
                r = await run_arm_mock(mock_url, n, c)
            else:
                r = await run_arm(url, n, c)
            results[(c, name)] = r
            lines.append(f"| {c} | {name} | {r['p50_ms']:.2f} | {r['p95_ms']:.2f} "
                         f"| {r['p99_ms']:.2f} | {r['rps']:.0f} |")
    ok = True
    for c in CONCURRENCIES:
        floor = results[(c, "direct-mock")]["p50_ms"]
        rust_oh = results[(c, "rust")]["p50_ms"] - floor
        py_oh = results[(c, "py")]["p50_ms"] - floor
        lines.append(f"| {c} | overhead rust/py | {rust_oh:.2f} | {py_oh:.2f} | | |")
        if py_oh > rust_oh + 3.0:
            ok = False
            lines.append(f"**FAIL: C={c} py overhead {py_oh:.2f}ms > rust {rust_oh:.2f}ms + 3ms**")
    rps_ratio = results[(32, "py")]["rps"] / results[(32, "rust")]["rps"]
    lines.append(f"\nRPS ratio @C=32 (py/rust): {rps_ratio:.2f} (threshold >= 0.70)")
    if rps_ratio < 0.70:
        ok = False
        lines.append("**FAIL: RPS ratio below 0.70**")
    lines.append(f"\n**B1 verdict: {'PASS' if ok else 'FAIL'}**")
    return lines


async def run_arm_mock(mock_url: str, n: int, concurrency: int) -> dict:
    latencies: list[float] = []
    sem = asyncio.Semaphore(concurrency)
    payload = {"model": "mock-model", "messages": [{"role": "user", "content": "x"}]}
    async with httpx.AsyncClient(timeout=30.0) as client:
        async def one() -> None:
            async with sem:
                t0 = time.perf_counter()
                r = await client.post(f"{mock_url}/v1/chat/completions", json=payload)
                r.raise_for_status()
                latencies.append(time.perf_counter() - t0)
        t0 = time.perf_counter()
        await asyncio.gather(*(one() for _ in range(n)))
        wall = time.perf_counter() - t0
    latencies.sort()
    q = statistics.quantiles(latencies, n=100)
    return {"p50_ms": q[49] * 1000, "p95_ms": q[94] * 1000,
            "p99_ms": q[98] * 1000, "rps": n / wall}


async def b2(real_llm_url: str) -> list[str]:
    rust = h._launch_rust(real_llm_url, [])
    py = h._launch_py(real_llm_url, [])
    try:
        bodies = [{"q": salted(t), "source": "auto", "target": "en"} for t in TEXTS]
        lines = ["| arm | p50 ms | p95 ms | p99 ms | RPS |", "|---|---|---|---|---|"]
        for name, srv in [("rust", rust), ("py", py)]:
            r = await run_arm(srv.base_url, len(bodies), 4, bodies=bodies)
            lines.append(f"| {name} | {r['p50_ms']:.0f} | {r['p95_ms']:.0f} "
                         f"| {r['p99_ms']:.0f} | {r['rps']:.2f} |")
        return lines
    finally:
        for s in (rust, py):
            s.proc.terminate()


async def b1() -> list[str]:
    rust_mock, _, _ = h.start_mock()
    py_mock, _, _ = h.start_mock()
    rust = h._launch_rust(rust_mock.base_url, [])
    py = h._launch_py(py_mock.base_url, [])
    try:
        return await bench_pair(rust.base_url, py.base_url, rust_mock.base_url, N_PER_ARM)
    finally:
        for s in (rust, py):
            s.proc.terminate()


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--b2", metavar="LLM_URL", help="run B2 against a real vLLM URL")
    args = ap.parse_args()
    lines = asyncio.run(b2(args.b2)) if args.b2 else asyncio.run(b1())
    print("\n".join(lines))
    return 1 if any("FAIL" in ln for ln in lines) else 0


if __name__ == "__main__":
    sys.exit(main())
```

- [ ] **Step 2: Run B1**

Run: `uv run --project backend python bench/bench_overhead.py`
Expected: table printed, `B1 verdict: PASS`. If FAIL: profile the Python hot path (obvious suspects: per-request `PromptBuilder`/detector setup, JSON serialization, logging at INFO) — optimize until PASS, each optimization re-validated against the full differential suite.

- [ ] **Step 3: Run B2** (requires the live container)

Run: `uv run --project backend python bench/bench_overhead.py --b2 http://localhost:8000`
Expected: table with rust/py within noise of each other (model dominates).

- [ ] **Step 4: Write `bench/REPORT.md`** — paste both tables plus: date, machine note ("Windows dev box, RTX 3090 host; desktop GPU contention affects both arms equally"), Rust ref commit (`git -C .rust-ref rev-parse --short HEAD`), model line from `GET http://localhost:8000/v1/models`, and the thresholds verdict.

- [ ] **Step 5: Live sanity tests**

`tests/live/test_live_sanity.py`:

```python
import pytest

from tests.differential.conftest import _launch_py, _launch_rust

LIVE_URL = "http://localhost:8000"


@pytest.mark.live
def test_live_translate_both_servers() -> None:
    rust = _launch_rust(LIVE_URL, [])
    py = _launch_py(LIVE_URL, [])
    try:
        import httpx
        for srv in (rust, py):
            r = httpx.post(f"{srv.base_url}/translate",
                           json={"q": "Привет, мир", "source": "auto", "target": "en"},
                           timeout=120.0)
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
        import httpx
        long_q = "The quick brown fox jumps over the lazy dog. " * 80  # ~3600 chars
        r = httpx.post(f"{py.base_url}/translate",
                       json={"q": long_q, "source": "en", "target": "ru"}, timeout=300.0)
        assert r.status_code == 200
        assert r.json()["translatedText"].strip()
    finally:
        py.proc.terminate()
```

Run: `uv run --project backend pytest tests/live -m live -v` — expected: PASS (needs the container up).

- [ ] **Step 6: Commit**

```bash
git add bench/ tests/live/
git commit -m "feat(bench): B1 wrapper-overhead + B2 e2e benchmarks with report"
```

---

### Task 14: One-command suite, README, final gate

**Files:**
- Create: `scripts/run_suite.py`
- Modify: `README.md`

- [ ] **Step 1: Write the orchestrator**

`scripts/run_suite.py`:

```python
"""One command: ensure Rust reference, then run the full non-live suite."""
from __future__ import annotations

import platform
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
EXE = "ltengine.exe" if platform.system() == "Windows" else "ltengine"
BIN = ROOT / ".rust-ref" / "target" / "release" / EXE


def main() -> int:
    if not BIN.exists():
        rc = subprocess.run([sys.executable, str(ROOT / "scripts" / "build_rust_ref.py")]).returncode
        if rc != 0:
            return rc
    return subprocess.run(["uv", "run", "--project", "backend", "pytest",
                           "tests", "-v"], cwd=ROOT).returncode


if __name__ == "__main__":
    sys.exit(main())
```

- [ ] **Step 2: Update README** — add a "Development" section: `uv run python scripts/run_suite.py` (full differential suite; builds the Rust reference on first run), `uv run --project backend pytest tests/live -m live` (needs `smartech-ltengine-vllm` up), `uv run --project backend python bench/bench_overhead.py` (B1) / `--b2 http://localhost:8000` (B2), and the new CLI flags table rows (`--llm-max-tokens` 0, `--llm-chars-per-token` 0.0, `--llm-max-tokens-mult` 0.0, `--llm-max-tokens-floor` 64, `--llm-timeout` 120). Update the project-structure block to the core/server layout.

- [ ] **Step 3: Final gate**

Run all of:
```bash
uv run python scripts/run_suite.py
uv run ruff check .
uv run mypy backend
```
Expected: suite green (zero xfails, zero skips other than live), ruff/mypy clean. Cross-check the spec §5 fix list item by item and §1 exit criteria; every box must have a passing test or committed artifact.

- [ ] **Step 4: Commit**

```bash
git add scripts/run_suite.py README.md
git commit -m "docs: one-command suite runner + README dev section (P1 gate complete)"
```

---

## Self-Review Notes

- **Spec coverage:** §4 harness → Tasks 3 (L1), 6 (L2), 13 (L3); §5 items 1→T6, 2→T4, 3–4→T5, 5→T8, 6→T6, 7–12→T7, 13→T11, 14→T10; §6 detection → T9; restructure → T12; §7 B1/B2 → T13; exit criteria → T14.
- **Rust-as-oracle rule:** wherever this plan sketches an expected Rust behavior (405s, serde messages, error strings, budget numbers), the differential diff is authoritative — transcribe observed values, never invent.
- **Type consistency:** `MockBehavior` fields, `ServerPair/ServerHandle`, `Snapshot`, `fire_both/assert_parity/last_payload`, `output_cap`, `LlmError`, `respond_with_grace`, `create_router` are each defined once (Tasks 3/5/6/8/12) and referenced with identical signatures everywhere else.
