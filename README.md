# LTBridge

Python port of [LTEngine](https://github.com/afanasjev82/LTEngine) `--features api` mode.

A LibreTranslate-compatible REST translation server that proxies requests to an external OpenAI-compatible LLM (e.g., vLLM, LM Studio, llama-server, Ollama, OpenAI). No local model loading — just a running LLM endpoint.

LTBridge is the productionized successor of the LTEngine api-mode port (formerly LTEngine-py).

### Backend compatibility

LTBridge is **inference-server agnostic**: it needs only `GET /v1/models` and
`POST /v1/chat/completions` (non-streaming) from an **instruct-tuned** model. Verified
end-to-end so far:

| Server | Model | Result |
|---|---|---|
| vLLM (CUDA) | `gemma-4-12b-it` (AWQ-INT4) | ✅ reference deployment; full differential parity suite |
| LM Studio (Apple MLX) | `qwen2.5-14b-instruct-mlx` | ✅ `/translate` verified live, zero code changes |
| LM Studio (Apple MLX) | `qwen/qwen3.8-27b` (4-bit, hybrid reasoning) | ✅ with `--llm-reasoning-effort none`; without it long texts come back untranslated (see note below) |

Notes:
- **Instruct models only** — the prompt is a system+user pair expecting a plain translated reply; base/completion-only models and reasoning-only endpoints won't return usable text.
- **Hybrid reasoning models** (Qwen3.x, QwQ, DeepSeek-R1 distills) — every request carries `chat_template_kwargs: {"enable_thinking": false}`, which vLLM/SGLang honour but **LM Studio ignores**. Left on, the model reasons at `temperature 0` (greedy decoding can loop for minutes), the call runs past `--llm-timeout`, and the response falls back to the **untranslated source text** with a `200` (the grace-race fallback, see `server/streaming.py`). Pass `--llm-reasoning-effort none` to switch thinking off there. A leading `<think>…</think>` block that a backend leaves inline in `content` is always stripped; an unterminated one is a `500` rather than reasoning passed off as a translation.
- **DeepSeek API** — not usable today: the served models (`deepseek-chat`/`deepseek-reasoner`) are not exposed as instruct-style completions in the way LTBridge's prompt expects, and the request carries the vLLM/SGLang `chat_template_kwargs: {"enable_thinking": false}` extension inherited from LTEngine (kept for byte-parity). Servers that reject unknown parameters would need an opt-in compat mode that omits it — not built yet.
- Add a row above whenever you verify a new backend.

`ltbridge` is a **byte-parity** port: its behavior is validated against a differential test suite that runs the Rust reference binary and this Python server side by side and diffs their responses (see [Development](#development)).

## Requirements

- Python 3.12+
- [uv](https://github.com/astral-sh/uv)
- An external OpenAI-compatible LLM server
- **WSL (Ubuntu-20.04) on Windows** — see [Development](#development); Windows itself can only edit source, not install/run/test

## Quick Start

```bash
# Install dependencies (WSL/Linux — see Development)
uv sync

# Start server (minimum: --llm-url is required)
uv run ltbridge --llm-url http://localhost:8000

# With explicit model and port
uv run ltbridge --llm-url http://localhost:8000 --llm-model my-model --port 5050
```

On startup, `ltbridge` prints a banner with build info (git SHA, granian/litestar
versions) and the applied config, then — if `--llm-model` is omitted — resolves the
model from `{llm-url}/v1/models` before binding. This resolve is **fail-fast**: it
**exits with code 1** if it can't get a model name back (e.g. the LLM endpoint is
unreachable), mirroring the Rust reference's startup behavior.

## API

Fully compatible with the [LibreTranslate API](https://libretranslate.com/docs/):

| Endpoint | Method | Description |
|---|---|---|
| `/translate` | POST | Translate text |
| `/detect` | POST | Detect language |
| `/languages` | GET | List supported languages |
| `/health` | GET | Health check |
| `/frontend/settings` | GET | Frontend config |
| `/metrics` | GET | Prometheus metrics (`ltengine_http_requests_total`, `ltengine_http_requests_duration_seconds` — actix_web_prom-compatible schema) |

### Translate

```bash
curl -X POST http://localhost:5050/translate \
  -H "Content-Type: application/json" \
  -d '{"q": "Hello!", "source": "en", "target": "es"}'
# {"translatedText": "¡Hola!"}
```

### Auto-detect Source Language

```bash
curl -X POST http://localhost:5050/translate \
  -H "Content-Type: application/json" \
  -d '{"q": "Ciao!", "source": "auto", "target": "en"}'
# {"detectedLanguage": {"confidence": 83, "language": "it"}, "translatedText": "Bye!"}
```

## Embedding LTBridge in another Litestar app

`ltbridge.server` re-exports its public embedding surface for a host app
(e.g. the smartech-translate proxy) to mount directly, sharing the host's
own pooled `httpx.AsyncClient` and reproducing byte-identical error bodies:

```python
from ltbridge.config import Config
from ltbridge.server import boundary, build_llm_client, create_router, exception_handlers

llm_client = build_llm_client(config, my_pooled_httpx_client)
inner = Litestar(route_handlers=[create_router(config, llm_client), my_own_route],
                 exception_handlers=exception_handlers())

# The outermost ASGI callable your server imports.
app = boundary(inner, extra_api_paths=frozenset({"/my/own/route"}))
```

`boundary()` is **required for parity**, not optional polish. The router alone
reproduces the response *bodies*; the semantics of every path the router does
*not* own live in an outer ASGI layer:

| request | with `boundary()` | router only |
|---|---|---|
| `POST /translate/` (trailing slash) | `405`, pinned body | **`200`** — Litestar slash-normalizes onto `/translate` |
| `GET /` | `200 text/html`, bundled UI | `404` |
| `GET /nope` | `404 Not found`, no `Content-Type` | `404` JSON-shaped by the host |

Only an **exact** match against `API_PATHS | extra_api_paths` reaches the inner
app, so every route the host owns must be listed in `extra_api_paths` (exact
strings — a trailing-slash variant is not implied). Pass `serve_static=False`
to drop the bundled UI and 404 the whole static surface instead. The wrapper
records **no metrics** — the host owns its own request accounting.

## CLI Options

| Flag | Default | Description |
|---|---|---|
| `--llm-url` | *(required)* | OpenAI-compatible API base URL |
| `--llm-model` | *(auto)* | Model name (auto-resolved from `/v1/models` if empty) |
| `--llm-api-key` | `` | API key for the LLM server |
| `--host` | `0.0.0.0` | Bind host |
| `--port`, `-p` | `5050` | Bind port |
| `--char-limit` | `5000` | Max characters per request |
| `--api-key` | `` | Optional API key to protect this server |
| `--llm-max-tokens` | `0` | Maximum output tokens per LLM request (`0` = no limit, field omitted) |
| `--llm-chars-per-token` | `0.0` | Dynamic token-budget cap (opt-in): characters-per-token divisor. Active only when this, `--llm-max-tokens`, and `--llm-max-tokens-mult` are all `> 0` |
| `--llm-max-tokens-mult` | `0.0` | Dynamic token-budget cap (opt-in): output safety multiple |
| `--llm-max-tokens-floor` | `64` | Dynamic cap: minimum output tokens for tiny inputs (ceiling still wins over floor) |
| `--llm-timeout` | `120` | HTTP timeout in seconds for LLM API requests (`0` = no timeout) |
| `--llm-reasoning-effort` | `` | Send OpenAI's `reasoning_effort` verbatim (`none`, `minimal`, `low`, `medium`, `high`). `none` disables thinking on LM Studio and recent vLLM. Empty = field omitted (Rust byte-parity); servers that reject the field (older vLLM, non-reasoning OpenAI models) keep working |
| `--workers` | `1` | Granian worker processes. **Warning:** `N>1` currently breaks `/metrics` — each worker is a separate process with its own metrics registry, so scraped counters are not aggregated and are not monotonically increasing. Leave at `1` for any deployment that scrapes `/metrics`; raise it only for a concurrency benchmark |
| `--verbose`, `-v` | off | Verbose logging |

## Project Structure

```
LTBridge/
├── backend/                  # Python package: ltbridge
│   └── src/ltbridge/
│       ├── core/              # transport-free, importable without Litestar
│       │   ├── prompt.py        # Translation prompt builder
│       │   ├── languages.py     # Language registry + whatlang-parity detection
│       │   ├── whatlang.py      # Pure-Python port of the whatlang 0.18 detector
│       │   ├── _whatlang_data.py  # GENERATED detection tables (see scripts/)
│       │   ├── token_budget.py  # Dynamic max_tokens budget calculation
│       │   ├── formatting.py    # Text formatting/trim helpers
│       │   └── errors.py        # LlmError taxonomy
│       ├── server/            # transport layer
│       │   ├── app.py           # Litestar application (create_app/app_factory)
│       │   ├── boundary.py      # Exported request boundary (static/404/405, exact-path gate)
│       │   ├── banner.py        # Startup ASCII banner + build/runtime/token-budget info
│       │   ├── routes.py        # Route handlers (create_router)
│       │   └── streaming.py     # Grace-race + heartbeat streaming
│       ├── config.py          # Config dataclass (CLI args <-> env <-> Config)
│       ├── llm_client.py      # OpenAI-compatible HTTP client
│       └── __main__.py        # CLI entry point + Granian server startup
├── frontend/                  # Static web UI (own uv workspace package)
│   └── src/ltbridge_frontend/static/
│       ├── index.html
│       ├── app.js
│       └── style.css
├── tests/
│   ├── unit/                   # Fast, no external process
│   ├── differential/            # Launches Rust oracle + Python server side by side
│   ├── live/                    # Needs a real vLLM container; `-m live`
│   └── fixtures/                # Recorded whatlang oracle outputs (detection sweep)
├── bench/                     # B1 (gated mock) + B2 (real vLLM) + sweep/real-bench extras
│   ├── bench_overhead.py    # B1 gated mock-upstream bench + B2 real-vLLM sanity bench
│   ├── real_bench.py        # Time-boxed ABAB real-vLLM throughput/latency bench
│   ├── sweep.py             # Wide-concurrency (up to C=64) mock characterization sweep
│   ├── texts.py             # Shared salted test corpus for the bench modules
│   └── REPORT.md            # Committed benchmark methodology + results
├── scripts/
│   ├── build_rust_ref.py       # Builds the Rust reference binary (WSL worktree)
│   ├── dump_rust_responses.py  # Manual debug: fires error cases at a running Rust server
│   ├── gen_whatlang_data.py    # Regenerates core/_whatlang_data.py from the crate
│   ├── sweep_detection_parity.py  # Zero-divergence gate for the detection port
│   └── run_suite.py            # One-command suite runner (WSL-aware)
└── docs/                      # Design specs and plans (local-only, gitignored)
```

## Development

**WSL is the supported development workflow.** The shipped server is pure Python — no
compiled extensions, no Rust. What still needs a native Rust toolchain is the
*differential test suite*, which proves byte-parity by running a compiled **Rust
reference binary** (the upstream [LTEngine](https://github.com/afanasjev82/LTEngine)
`development` branch, built locally into the gitignored `.rust-ref/`) side by side with
this Python server. On this project's dev environment the native Windows Rust/MSVC
toolchain is broken, so do all dependency installation, building, testing, and running
under **WSL (Ubuntu-20.04)**; Windows is edit-only (browsing/editing source, docs). Every
command below must be run from the **repo root** — the root `pyproject.toml`'s
`[tool.pytest.ini_options]`/`[tool.mypy]` paths are resolved relative to the current
working directory.

```bash
# Install dependencies
uv sync

# Build/refresh the Rust reference oracle in .rust-ref/ (idempotent; safe to re-run —
# run_suite.py below calls this automatically the first time it's needed)
uv run python scripts/build_rust_ref.py

# Run the full non-live differential + unit suite. One command works from EITHER side:
# on Windows it re-execs itself inside `wsl -d Ubuntu-20.04` automatically; under WSL/
# Linux it runs directly. Ensures the Rust oracle exists first, then fails loudly if the
# suite reports ANY skipped tests (a skip there means the oracle was missing/broken, not
# a clean pass).
uv run python scripts/run_suite.py

# Equivalent, once synced and the oracle is built:
uv run pytest tests

# Live sanity tests against a real vLLM container (smartech-ltengine-vllm), excluded from
# the default run. Override the URL with LTBRIDGE_BENCH_LLM_URL when localhost:8000 isn't
# reachable from inside WSL on this host.
uv run --project backend pytest tests/live -m live -v

# B1 — wrapper-overhead benchmark against the Rust reference on a zero-delay mock
# upstream (no LLM needed). --runs sets the number of C=32 repeats used for the
# median-RPS gate; the amended gate requires >= 5 (enforced by the script).
uv run --project backend python bench/bench_overhead.py --runs 5

# B2 — end-to-end sanity benchmark against a real vLLM upstream (default
# http://localhost:8000, or --b2 URL for an explicit one; informational, not gated).
uv run --project backend python bench/bench_overhead.py --b2
uv run --project backend python bench/bench_overhead.py --b2 http://localhost:8000

# Extended mock-upstream concurrency sweep (C up to 64) — characterization only, does
# NOT touch bench_overhead.py's gate. One bounded, foreground call per concurrency:
uv run --project backend python -m bench.sweep --concurrencies 16 --results bench/_sweep.json
uv run --project backend python -m bench.sweep --report --results bench/_sweep.json --out bench/_sweep_out.md

# Real-vLLM ABAB throughput/latency bench (informational, not gated). One bounded,
# foreground call per (round, C) cell pair against $LTBRIDGE_BENCH_LLM_URL (default
# http://localhost:8000):
uv run --project backend python -m bench.real_bench --round 1 --c 1 --warmup --results bench/_real_bench.json
uv run --project backend python -m bench.real_bench --assemble --results bench/_real_bench.json --out bench/_real_bench_out.md

# Lint / type-check (must both be clean before merge)
uv run ruff check .
uv run mypy backend
```

### Language detection

Offline language detection is a **pure-Python port** of the Rust
[`whatlang`](https://crates.io/crates/whatlang) 0.18.0 crate, living in
`backend/src/ltbridge/core/whatlang.py`. It replaced a vendored PyO3/maturin binding, so
the repo now contains no Rust and `uv sync` needs no compiler.

All of its tables — trigram profiles, per-script Unicode ranges, alphabets, `Lang`
metadata, script→language mappings — are **generated**, never hand-transcribed:

```bash
# Regenerate backend/src/ltbridge/core/_whatlang_data.py from the crate source
# (auto-locates ~/.cargo/registry/src/*/whatlang-0.18.0; needs rustc only to snapshot
#  the Rust std case tables, and writes no Rust into this repo)
uv run python scripts/gen_whatlang_data.py

# Re-run the zero-divergence gate against the recorded oracle outputs
uv run python scripts/sweep_detection_parity.py

# Re-prove against the real crate and re-record the fixture. Only needed after a
# crate bump, and it requires temporarily reinstating the whatlang-py PyO3 binding
# as a uv workspace member (deleted in favour of the port).
uv run python scripts/sweep_detection_parity.py --oracle binding --dump
```

The generated module's header records the crate version, the rustc version and a
SHA-256 of every parsed crate file. Parity is guarded from two directions: the
differential suite fires `/detect` at the live Rust oracle, and
`tests/unit/test_detection_sweep.py` replays 16 369 recorded oracle outputs
(`tests/fixtures/detection_sweep.json.gz`) covering every script, all 49 profiled
languages, degenerate inputs and single characters across the range tables.

Two deliberate fidelity choices are documented in the module: sorting uses Python's
stable `sorted` (exact, because Rust's `sort_unstable` is stable at the slice lengths
involved), and lowercasing uses a snapshot of the **Rust** standard library's case
tables rather than `str.lower()` — rustc 1.96 ships Unicode 16.0 against CPython 3.12's
15.0, and pinning stops a future CPython Unicode bump from moving detection.

## Benchmarks

Results (methodology, environment, and raw numbers) are committed to
[`bench/REPORT.md`](bench/REPORT.md). Summary of the current gate:

- **B1 (wrapper overhead):** C=1 gated at `py_p50 <= rust_p50 + 3ms` — **PASS**. C=32
  gated on `median(py_rps) / median(rust_rps) >= 0.60` over >= 5 runs against an
  out-of-process mock upstream (re-anchored 2026-08-15 — the prior 0.70 bar was measured
  against an in-process mock that self-saturated and inflated the ratio to 0.84–0.91;
  the honest out-of-process ratio is ~0.66) — **PASS**. C=8 and C=32 p50/p95/p99 are
  printed as informational-only.
- **B2 (e2e sanity vs. real vLLM):** informational — latency parity within noise, model
  dominates end-to-end time. `bench/sweep.py` (wide mock-concurrency characterization)
  and `bench/real_bench.py` (time-boxed ABAB real-vLLM throughput) extend this further;
  both are characterization only, not gated.
