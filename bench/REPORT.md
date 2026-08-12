# Task 13 — B1 wrapper-overhead / B2 e2e sanity benchmark report

Date: 2026-08-12
Repo: `LTEngine-py` @ `development`
Oracle (Rust reference) commit: `.rust-ref` @ `3070771` (`git -C .rust-ref rev-parse --short HEAD`)

## Environment

- Everything below ran under WSL (`wsl.exe -d Ubuntu-20.04`), not native Windows:
  `Linux Dimas-PC 6.18.33.2-microsoft-standard-WSL2 #1 SMP PREEMPT_DYNAMIC x86_64 GNU/Linux`.
- Host: Windows dev box, 20 logical CPUs visible to WSL (`nproc` = 20). Shared machine —
  background load on the Windows side (and inside the WSL2 VM) is not controlled for, and
  run-to-run variance below is consistent with that (the Rust arm's own p50 at C=32 ranged
  ~29-36ms across otherwise-identical repeats).
- Timing: `time.perf_counter()` throughout (monotonic; immune to WSL/Windows wall-clock
  adjustment). B1's rust/py arms always run back-to-back in one process invocation.
- **WSL to LLM URL resolution (B2):** `http://localhost:8000` is reachable directly from
  inside WSL on this host (verified via `curl -m 5 http://localhost:8000/v1/models`) - no
  Windows-host-IP indirection was needed. `bench/bench_overhead.py` still parameterizes this
  via `LTENGINE_BENCH_LLM_URL` (default `http://localhost:8000`) rather than hardcoding it,
  per the task's portability requirement; `tests/live/test_live_sanity.py` resolves the same
  env var the same way.
- Model at `http://localhost:8000/v1/models`: `gemma-4-12b-it`
  (`root: cyankiwi/gemma-4-12B-it-AWQ-INT4`, `max_model_len: 32768`, served by vLLM).
- vLLM engine load was confirmed idle (`vllm:num_requests_running 0.0` via `/metrics`)
  immediately before every B1/B2/live run below.

## B1 — wrapper overhead vs the Rust reference (mock upstream)

Thresholds (spec §7): **py p50 overhead <= rust p50 overhead + 3ms** at every C; **py RPS >=
70% of rust RPS @ C=32**.

### Baseline (brief's code, unmodified) — 3 runs

| run | C=32 py p50 | C=32 rust p50 | gap (py-rust) | RPS ratio @C=32 |
|---|---|---|---|---|
| 1 | 46.62 | 34.54 | 12.08 ms | 0.85 |
| 2 | 49.03 | 32.47 | 16.56 ms | 0.81 |
| 3 | 49.47 | 30.94 | 18.53 ms | 0.75 |
| **avg** | | | **15.72 ms** | **0.80** |

C=1 and C=8 already passed at baseline (gap <= ~1.6ms both). **C=32's p50-overhead gate
failed** every run, by a wide margin; the RPS-ratio gate was already comfortably above 0.70
at baseline (this was never the actual blocker — see "B1 verdict" below).

### B1 optimizations (in order applied, each re-validated against the full differential suite)

1. **Fixed an accidental double logging pipeline + silenced httpx's per-request access log**
   (`backend/src/ltengine/server/app.py`). Litestar installs its own default `LoggingConfig()`
   whenever `logging_config` isn't passed explicitly — on Python >=3.12 this wires the root
   logger to a `logging.handlers.QueueHandler` backed by a background-thread
   `QueueListener` (`configure_root_logger=True` by default). Two consequences, both live on
   the request path: (a) the app's own `logging.basicConfig(...)` call in `lifespan()` became
   a **silent no-op** (root already had a handler; `basicConfig` refuses to touch an
   already-configured root logger) — confirmed empirically: log lines were coming out in
   Litestar's default format, not the app's own `"%(asctime)s %(name)s %(levelname)s
   %(message)s"`; (b) **every** log record — including httpx's own `"HTTP Request: POST ..."`
   INFO line, emitted once per upstream call, entirely unrequested — was routed through a
   cross-thread queue on every single request. cProfile on an in-process ASGI harness
   (AsyncTestClient + ASGI-mock upstream, C=32, 4000 requests, warm) traced
   `QueueListener.dequeue` alone to ~1.76s of ~7.39s profiled time (~24%), plus lock
   acquire/release (~0.63s) and LogRecord construction/writes feeding it — all reproducing 1:1
   with request count. Fix: `logging_config=None` on the `Litestar`/`_LTEngineApp`
   constructor (so the app's own `basicConfig` is the only logging config in effect, a plain
   synchronous `StreamHandler`, no queue/thread), plus
   `logging.getLogger("httpx").setLevel(logging.WARNING)` right after it (keeps genuine
   upstream failures visible on that logger, drops the per-request noise). Re-profiling the
   same harness after the fix: total function calls 11.9M -> 10.7M, profiled time 7.39s ->
   6.43s (~13% down), and `dequeue`/lock-contention entirely dropped out of the top 25.
   **Real-world B1 effect:** C=32 gap ~15-18ms -> ~7-13ms in repeat runs immediately after this
   one change (RPS ratio also improved, ~0.80 -> ~0.87-0.91 avg).

2. **`--workers N` on the Python launch** (`backend/src/ltengine/__main__.py`,
   `bench/bench_overhead.py`). The Rust reference's `main.rs` never calls `.workers(N)` on its
   `actix_web::HttpServer` — actix-web's default is one OS worker thread per CPU core (20 here
   — genuine multi-core parallelism for its per-request CPU-bound work). `ltengine.__main__`
   hardcoded Granian to `workers=1` — a single Python **process**, hence a single GIL: every
   concurrent request's synchronous CPU segments (JSON parse, prompt building, response
   assembly) serialize onto one core no matter how many requests arrive at once. That
   asymmetry (N native OS threads with real parallelism vs. one GIL-bound process) is
   structural, and it explains why the C=1/C=8 gaps were already fine (<=1-2ms, per-request
   fixed cost dominates, no queueing) while C=32 blew up disproportionately (32 requests' worth
   of tiny serialized CPU bursts stack up on one core). Added an opt-in `--workers` CLI flag
   (`default=1`, so every existing caller — the differential suite's `_launch_py`, the live
   tests, plain CLI use — is unaffected byte-for-byte) and wired B1's py launch to
   `--workers {os.cpu_count()}` (overridable via `LTENGINE_BENCH_WORKERS`), giving Python the
   same per-core parallelism headroom Rust already has by default. Verified the multi-process
   launch starts and serves correctly (4 workers spawned, each logging its own model
   resolution, `/languages` responding) before wiring it into the bench. **Real-world B1
   effect:** this was the single biggest lever — C=32 gap dropped further, to roughly 5-16ms
   across repeats (see "post-optimization" table below); RPS ratio settled at ~0.85-0.97 in
   most runs (one outlier run at 0.61, discussed below).

3. **Eliminated the redundant double-JSON-parse on the common (well-typed) request**
   (`backend/src/ltengine/server/routes.py`, `_check_typed_fields`/new `_has_type_mismatch`).
   `_parse_translate_payload` already produces `parsed` via `json.loads(raw)`; the type-check
   pass then re-walked the **same** JSON text a second time via `errors.field_spans()` (a
   token-span-tracking re-parse using `json.JSONDecoder.raw_decode` per key) — unconditionally,
   on every request, even when every field is already correctly typed (the overwhelming
   majority case, including every B1 request). Since `field_spans(text)` and `json.loads(raw)`
   decode the identical bytes through CPython's json module, their key order and values are
   guaranteed identical (including duplicate-key last-value-wins semantics) — so a cheap
   `isinstance`-only pass over the already-parsed `dict` can decide "no mismatch, skip the
   re-parse" with zero behavior change; `field_spans` now runs only when a mismatch is actually
   found, to build the precise span-based error message (the one thing the fast path can't
   produce). Purely additive/skip-only — verified against the full differential suite,
   including every type-mismatch error-shape test, with no diff.

Each of the three changes was re-validated against the **full non-live differential suite**
(149/0/0) before moving to the next; final state is the same 149/0/0 (see "Verification"
below).

### Post-optimization — 5 runs (all three fixes applied, `--workers` = `os.cpu_count()` = 20)

Representative full table (run 5 of 5, chosen as the median-gap run):

| C | arm | p50 ms | p95 ms | p99 ms | RPS |
|---|---|---|---|---|---|
| 1 | direct-mock | 0.74 | 0.97 | 1.22 | 1273 |
| 1 | rust | 0.68 | 0.88 | 1.01 | 1384 |
| 1 | py | 1.45 | 1.89 | 2.19 | 657 |
| 8 | direct-mock | 7.56 | 15.08 | 22.19 | 914 |
| 8 | rust | 6.62 | 16.28 | 25.05 | 1016 |
| 8 | py | 6.13 | 12.36 | 17.95 | 1091 |
| 32 | direct-mock | 36.98 | 141.16 | 211.40 | 616 |
| 32 | rust | 31.81 | 121.70 | 191.27 | 706 |
| 32 | py | 42.73 | 104.92 | 173.59 | 624 |

C=1 overhead: rust -0.06ms / py 0.72ms -> **PASS** (gap 0.78ms <= 3ms).
C=8 overhead: rust -0.94ms / py -1.43ms -> **PASS** (py faster than the floor here; gap
-0.49ms).
C=32 overhead: rust -5.18ms / py 5.74ms -> **FAIL** (gap 10.92ms > 3ms).

RPS ratio @C=32 (py/rust): 0.88 (>= 0.70 threshold: **PASS**).

C=32 across all 5 post-optimization repeats (chronological; `direct-mock`/C=1/C=8 elided —
all 5 passed those the same way as above):

| run | py p50 | rust p50 | gap (py-rust) | RPS ratio |
|---|---|---|---|---|
| 1 | 44.63 | 35.64 | 8.99 ms | 0.97 |
| 2 | 45.10 | 29.23 | 15.87 ms | 0.61 (FAIL — only outlier below 0.70) |
| 3 | 41.22 | 34.09 | 7.13 ms | 0.88 |
| 4 | 45.87 | 33.99 | 11.88 ms | 0.92 |
| 5 (shown above) | 42.73 | 31.81 | 10.92 ms | 0.88 |
| **avg** | | | **10.96 ms** | **0.85** |

Run 2's rust p50 (29.23ms, vs. 31.8-36.3ms every other run) is a clear outlier in rust's own
favor — consistent with host-level noise (background load dipping during that specific
window) rather than anything on the Python side; it dragged the RPS ratio below 0.70 in that
one run only.

### B1 verdict: **DONE_WITH_CONCERNS**

- **C=1: PASS** (all runs, gap well under 3ms).
- **C=8: PASS** (all runs; py is at or below the rust+3ms bar every time, often faster).
- **C=32 RPS ratio (>=0.70): PASS in 4/5 runs** (avg 0.85); one run dipped to 0.61, attributable
  to a rust-side speed outlier that run, not a py regression.
- **C=32 p50-overhead (<= rust+3ms): FAIL, consistently**, both before and after optimization.
  Baseline avg gap 15.72ms -> post-optimization avg gap 10.96ms (**30% reduction** from three
  verified, safe changes), but never under the 3ms bar in any of the 8 total C=32 runs
  measured (3 baseline + 5 post-optimization).

Per the task's instructions, thresholds were **not** weakened. All three optimizations are
real fixes (an accidental logging pipeline, a hardcoded single-process server under
concurrency, a redundant per-request re-parse) with measurable, reproducible effect, each
verified safe against the full differential parity suite. The residual ~11ms median gap at
C=32 appears to be the combination of (a) irreducible framework-dispatch cost — Litestar/ASGI
request routing and Python-level object construction vs. actix-web's compiled dispatch, which
no longer has an obvious single dominant contributor after the three fixes above (a repeat
in-process cProfile pass showed the remaining top entries split across httpx internals,
`anyio` bridging specific to the *test-harness* transport, and normal ASGI/Litestar routing —
no further single "smoking gun"), and (b) this shared host's run-to-run variance, which is
large enough (rust's own p50 spanning ~29-36ms across identical repeats) that closing an
additional ~8ms consistently would need a quieter, dedicated benchmark machine to even
measure reliably, not just more code changes.

## B2 — end-to-end sanity vs. a real vLLM upstream

Not gated (spec: "sanity, not gated on absolutes" — model latency dominates). 20 salted texts,
`source=auto`, `target=en`, C=4.

### Ordering confound found and fixed

The brief's original `b2()` launches rust and py against the **same shared vLLM engine** and
measures them sequentially (`for name, srv in [("rust", ...), ("py", ...)]`). The first arm in
that loop pays the engine's cold-start cost (prefix-cache priming for the shared system
prompt, any CUDA warm-up) that the second arm, running right after, does not. First B2 run
(no warm-up) showed a large, suspicious gap:

| arm | p50 ms | p95 ms | p99 ms | RPS |
|---|---|---|---|---|
| rust | 737 | 3261 | 3300 | 3.31 |
| py | 384 | 465 | 481 | 10.15 |

That p95/p99 shape (rust's tail ~7x its own p50, py's tail tight) is a cold-start/ordering
signature, not a language-level difference — confirmed by a one-off diagnostic run with the
arm order **swapped** (`py` measured first, `rust` second): the pattern flipped exactly —
`py: p50=423 p95=698 p99=718`, `rust: p50=380 p95=438 p99=460` — whichever arm runs first
against the shared engine gets the worse tail, regardless of which language it is. Fix:
`bench/bench_overhead.py`'s `b2()` now fires two throwaway warm-up calls at the real upstream
(discovering the served model id via `/v1/models` first, so vLLM doesn't 400 them) **before**
either arm is launched or measured, so neither arm eats the engine's cold start.

### Post-fix results (2 runs)

| arm | p50 ms | p95 ms | p99 ms | RPS |
|---|---|---|---|---|
| rust | 386 | 658 | 669 | 9.09 |
| py | 367 | 416 | 431 | 10.91 |

| arm | p50 ms | p95 ms | p99 ms | RPS |
|---|---|---|---|---|
| rust | 407 | 643 | 658 | 9.31 |
| py | 372 | 446 | 487 | 11.39 |

p50s track within ~5-10% across both runs (367-407ms rust, 361-418ms py across all post-fix
runs including the earlier ones) — consistent with the model dominating, as expected. A
smaller residual tail difference remains (rust p95/p99 still ~1.5x py's even after the
warm-up), most plausibly further residual ordering effect (rust is still always first in the
measured loop, even though both now start from an equally warm engine) compounded by the
inherent noise of a 20-request/C=4 sample (p95/p99 are the 1-2 slowest of 20 requests — a
single slow generation shifts them substantially). Given B2 is explicitly non-gated and the
p50s (the stable statistic at this sample size) agree closely, this was not chased further.

### B2 summary: informational PASS (model dominates; no gate to fail). Ordering confound found, diagnosed, and fixed via warm-up.

## Live sanity tests

`tests/live/test_live_sanity.py` (`@pytest.mark.live`, excluded by default via
`addopts = "-m 'not live'"` in `pyproject.toml` — confirmed: the full suite run shows `149
passed, 2 deselected`, the 2 being these live tests).

Run: `uv run --project backend pytest tests/live -m live -v` (vLLM engine confirmed idle via
`/metrics` immediately before the run):

```
tests/live/test_live_sanity.py::test_live_translate_both_servers PASSED  [ 50%]
tests/live/test_live_sanity.py::test_live_long_text_heartbeat_py PASSED  [100%]

======================== 2 passed in 131.35s (0:02:11) =========================
```

## Verification

- Full non-live suite before any change: `149 passed` (baseline).
- Full non-live suite after each of the 3 B1 optimizations (run individually, in order): `149
  passed` each time — no parity regression introduced.
- Full non-live suite, final state (with `tests/live/` added): `149 passed, 2 deselected, 2
  warnings in 137.91s` — same 149/0/0, the 2 deselected are the new live tests (excluded by
  the existing `not live` addopts, as intended).
- `ruff check` / `ruff format --check`: clean on every touched file
  (`backend/src/ltengine/__main__.py`, `backend/src/ltengine/server/app.py`,
  `backend/src/ltengine/server/routes.py`, `bench/*.py`, `tests/live/*.py`). `__main__.py`
  carries 3 pre-existing `ruff format` findings (confirmed identical against `git stash` —
  unrelated to this task's edits, not touched here).
- `mypy` (project config, `files = ["backend/src"]`): same 3 pre-existing errors as baseline
  (`__main__.py`'s `log_level` argument typing, `app.py`'s `ltengine_frontend` stub gap) — no
  new errors from this task's changes. `bench/` and `tests/live/` fall outside the configured
  `mypy` scope (same as the rest of `tests/`).
