# ltengine-py

Python port of [LTEngine](https://github.com/afanasjev82/LTEngine) `--features api` mode.

A LibreTranslate-compatible REST translation server that proxies requests to an external OpenAI-compatible LLM (e.g., vLLM, llama-server, Ollama, OpenAI). No local model loading — just a running LLM endpoint.

`ltengine-py` is a **byte-parity** port: its behavior is validated against a differential test suite that runs the Rust reference binary and this Python server side by side and diffs their responses (see [Development](#development)).

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
uv run ltengine --llm-url http://localhost:8000

# With explicit model and port
uv run ltengine --llm-url http://localhost:8000 --llm-model my-model --port 5050
```

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
# {"translatedText": "Bye!", "detectedLanguage": {"language": "it", "confidence": 83}}
```

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
| `--workers` | `1` | Granian worker processes. **Warning:** `N>1` currently breaks `/metrics` — each worker is a separate process with its own metrics registry, so scraped counters are not aggregated and are not monotonically increasing. Leave at `1` for any deployment that scrapes `/metrics`; raise it only for a concurrency benchmark |
| `--verbose`, `-v` | off | Verbose logging |

## Project Structure

```
ltengine-py/
├── backend/                  # Python package: ltengine
│   └── src/ltengine/
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
│       │   ├── routes.py        # Route handlers (create_router)
│       │   └── streaming.py     # Grace-race + heartbeat streaming
│       ├── config.py          # Config dataclass (CLI args <-> env <-> Config)
│       ├── llm_client.py      # OpenAI-compatible HTTP client
│       └── __main__.py        # CLI entry point + Granian server startup
├── frontend/                  # Static web UI (own uv workspace package)
│   └── src/ltengine_frontend/static/
│       ├── index.html
│       ├── app.js
│       └── style.css
├── tests/
│   ├── unit/                   # Fast, no external process
│   ├── differential/            # Launches Rust oracle + Python server side by side
│   ├── live/                    # Needs a real vLLM container; `-m live`
│   └── fixtures/                # Recorded whatlang oracle outputs (detection sweep)
├── bench/                     # B1 (mock upstream) + B2 (real vLLM) benchmarks
├── scripts/
│   ├── build_rust_ref.py       # Builds the Rust reference binary (WSL worktree)
│   ├── gen_whatlang_data.py    # Regenerates core/_whatlang_data.py from the crate
│   ├── sweep_detection_parity.py  # Zero-divergence gate for the detection port
│   └── run_suite.py            # One-command suite runner (WSL-aware)
└── docs/                      # Design specs and plans (docs/superpowers/)
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
# the default run. Override the URL with LTENGINE_BENCH_LLM_URL when localhost:8000 isn't
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

# Lint / type-check (must both be clean before merge)
uv run ruff check .
uv run mypy backend
```

### Language detection

Offline language detection is a **pure-Python port** of the Rust
[`whatlang`](https://crates.io/crates/whatlang) 0.18.0 crate, living in
`backend/src/ltengine/core/whatlang.py`. It replaced a vendored PyO3/maturin binding, so
the repo now contains no Rust and `uv sync` needs no compiler.

All of its tables — trigram profiles, per-script Unicode ranges, alphabets, `Lang`
metadata, script→language mappings — are **generated**, never hand-transcribed:

```bash
# Regenerate backend/src/ltengine/core/_whatlang_data.py from the crate source
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
(`tests/fixtures/detection_sweep.json.gz`) covering every script, all 51 profiled
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
  gated on `median(py_rps) / median(rust_rps) >= 0.70` over >= 5 runs — **PASS**
  (ratios 0.84–0.91). C=8 and C=32 p50/p95/p99 are printed as informational-only.
- **B2 (e2e sanity vs. real vLLM):** informational — latency parity within noise, model
  dominates end-to-end time.
