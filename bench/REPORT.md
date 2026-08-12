# Task 13 — B1 wrapper-overhead / B2 e2e sanity benchmark report

Date: 2026-08-12 (original investigation) / 2026-08-12 (fix wave, amended gate)
Repo: `LTEngine-py` @ `development`
Oracle (Rust reference) commit: `.rust-ref` @ `3070771` (`git -C .rust-ref rev-parse --short HEAD`)

## Environment

- Everything below ran under WSL (`wsl.exe -d Ubuntu-20.04`), not native Windows:
  `Linux Dimas-PC 6.18.33.2-microsoft-standard-WSL2 #1 SMP PREEMPT_DYNAMIC x86_64 GNU/Linux`.
- **Machine note:** Windows dev box, 20 logical CPUs visible to WSL (`nproc` = 20), with an
  **NVIDIA GeForce RTX 3090** (confirmed via `nvidia-smi`: 23.7/24.5 GiB VRAM resident — the
  local vLLM instance's model weights + KV cache — 6% utilization while idle). vLLM (serving
  `gemma-4-12b-it`) runs locally on this same box/GPU, so **desktop GPU contention affects
  both arms equally** (rust and py dispatch to the same shared engine instance; neither is
  privileged). This is a shared, uncontrolled machine — background load on the Windows side
  and inside the WSL2 VM is not controlled for, and the run-to-run variance documented below
  (rust's own C=32 p50 spanning ~29-36ms across otherwise-identical repeats) is consistent
  with that.
- Timing: `time.perf_counter()` throughout (monotonic; immune to WSL/Windows wall-clock
  adjustment). B1's arms always run back-to-back in one process invocation.
- **WSL to LLM URL resolution (B2):** `http://localhost:8000` is reachable directly from
  inside WSL on this host (verified via `curl -m 5 http://localhost:8000/v1/models`) — no
  Windows-host-IP indirection was needed. `bench/bench_overhead.py` still parameterizes this
  via `LTENGINE_BENCH_LLM_URL` (default `http://localhost:8000`) rather than hardcoding it;
  `tests/live/test_live_sanity.py` resolves the same env var the same way.
- Model at `http://localhost:8000/v1/models`: `gemma-4-12b-it`
  (`root: cyankiwi/gemma-4-12B-it-AWQ-INT4`, `max_model_len: 32768`, served by vLLM).
- vLLM engine load was confirmed idle (`vllm:num_requests_running 0.0` via `/metrics`)
  immediately before every timed B1/B2/live run below.

## Amended gate (2026-08-12, review + human amendment, commit `e5abca1`)

The original bar — "py p50 overhead ≤ rust p50 overhead + 3ms" at every concurrency, plus
"py RPS ≥ 70% rust RPS @ C=32" — was reviewed and found unphysical **at C=32 specifically**:
the in-process mock upstream itself saturates under that much concurrency (~642rps/35ms
observed even with a completely empty body — a property of the harness, not of either
server), the floor-subtraction used to compute "overhead" cancels out algebraically in the
pass/fail comparison either way (so it added confusion without changing the verdict), and the
resulting 3ms bar sat below this benchmark's own run-to-run instrument noise (rust's own p50
swung ~7ms across identical repeats — see below). The amended gate, implemented in
`bench/bench_overhead.py`:

- **C=1** (single run): still GATED, `py_p50 ≤ rust_p50 + 3.0ms`. No saturation at C=1 (RPS
  in the 1000+ range, no queueing), so the bar is physical here and stays.
- **C=8** (single run): informational only, no gate. Never actually failed at any point in
  this investigation, but sits on the same saturation trajectory as C=32 (just less severe) —
  not worth defending a second unphysical bar.
- **C=32** (`--runs N`, default 5, ≥5 required): GATED on `median(py_rps) / median(rust_rps)
  ≥ 0.70` across the repeats — the median averages out the saturation noise a single run
  can't. p50/p95/p99 are still printed per run, explicitly labeled INFORMATIONAL, so they
  stay visible without being load-bearing.

Two harness fixes shipped alongside the gate change (details in "B1 ordering discipline" and
"B2 prefix-cache confound" below): the three arms (direct-mock, rust, py) are now measured in
a **randomized order** on every round, each arm gets a small **per-arm warmup** batch before
its timed run, and subprocess teardown now matches `tests/differential/conftest.py`'s pattern
(`terminate()` → `wait(timeout=10)` → `kill()` on timeout) everywhere a bench or live-test
server is torn down.

---

## B1 — original investigation (pre-amendment; historical record)

This section is the original investigation that produced the fixes below, run under the
**original, now-retired** gate (`py p50 overhead ≤ rust p50 overhead + 3ms` at every C). Kept
for the debugging narrative and because the three optimizations it produced are still in the
codebase and still measurably helpful. See "B1 — fresh results (amended gate)" further down
for the current, authoritative verdict.

### Baseline (brief's code, unmodified) — 3 full runs

| run | C | arm | p50 ms | p95 ms | p99 ms | RPS |
|---|---|---|---|---|---|---|
| 1 | 1 | direct-mock | 0.71 | 0.92 | 1.09 | 1331 |
| 1 | 1 | rust | 0.73 | 0.92 | 1.07 | 1314 |
| 1 | 1 | py | 1.74 | 2.19 | 2.52 | 556 |
| 1 | 8 | direct-mock | 8.38 | 16.99 | 24.96 | 822 |
| 1 | 8 | rust | 7.23 | 17.43 | 27.23 | 930 |
| 1 | 8 | py | 9.38 | 13.25 | 16.44 | 814 |
| 1 | 32 | direct-mock | 39.30 | 147.52 | 216.83 | 572 |
| 1 | 32 | rust | 34.54 | 122.94 | 193.08 | 661 |
| 1 | 32 | py | 46.62 | 115.91 | 176.08 | 565 |
| 2 | 1 | direct-mock | 0.59 | 0.78 | 0.89 | 1573 |
| 2 | 1 | rust | 0.70 | 0.91 | 1.03 | 1354 |
| 2 | 1 | py | 1.61 | 2.06 | 2.29 | 599 |
| 2 | 8 | direct-mock | 7.88 | 16.39 | 25.45 | 861 |
| 2 | 8 | rust | 7.15 | 16.60 | 26.38 | 962 |
| 2 | 8 | py | 9.48 | 12.94 | 16.51 | 817 |
| 2 | 32 | direct-mock | 35.60 | 139.88 | 202.10 | 616 |
| 2 | 32 | rust | 32.47 | 124.03 | 189.07 | 673 |
| 2 | 32 | py | 49.03 | 122.86 | 166.39 | 547 |
| 3 | 1 | direct-mock | 0.69 | 0.89 | 1.04 | 1361 |
| 3 | 1 | rust | 0.66 | 0.94 | 1.14 | 1395 |
| 3 | 1 | py | 1.61 | 2.02 | 2.24 | 605 |
| 3 | 8 | direct-mock | 8.26 | 17.31 | 25.71 | 817 |
| 3 | 8 | rust | 6.92 | 15.29 | 24.36 | 996 |
| 3 | 8 | py | 9.65 | 13.57 | 17.11 | 795 |
| 3 | 32 | direct-mock | 34.99 | 134.13 | 201.89 | 641 |
| 3 | 32 | rust | 30.94 | 120.25 | 183.13 | 724 |
| 3 | 32 | py | 49.47 | 119.72 | 172.16 | 542 |

Derived gaps (`py_p50 − rust_p50`; this is what the old bar actually gated on — the
`direct-mock` floor cancels out of the pass/fail arithmetic algebraically, it only mattered
for display):

| run | C=1 gap | C=8 gap | C=32 gap | C=32 RPS ratio (py/rust) |
|---|---|---|---|---|
| 1 | 1.01 ms | 2.15 ms | 12.08 ms | 0.85 |
| 2 | 0.91 ms | 2.33 ms | 16.56 ms | 0.81 |
| 3 | 0.95 ms | 2.73 ms | 18.53 ms | 0.75 |
| **avg** | **0.96 ms** | **2.40 ms** | **15.72 ms** | **0.80** |

(Correction: an earlier draft of this report claimed C=1/C=8 baseline gaps of "≤~1.6ms both"
without publishing the underlying numbers — the actual max was 2.73ms at C=8, run 3, per the
table above. Both were still comfortably under the 3ms bar; the claim's *conclusion* was
right, its evidence wasn't shown. Fixed here.)

C=1 and C=8 passed the old bar every run. **C=32's p50-overhead gate failed every run**, by a
wide margin; the RPS-ratio gate was already comfortably above 0.70 at baseline — it was never
the actual blocker (this is also why the amended gate keeps the RPS-ratio check and drops the
p50 one, rather than the reverse).

### Optimizations applied (in order, each re-validated against the full differential suite)

1. **Fixed an accidental double logging pipeline + silenced httpx's per-request access log**
   (`backend/src/ltengine/server/app.py`). Litestar installs its own default `LoggingConfig()`
   whenever `logging_config` isn't passed explicitly — on Python ≥3.12 this wires the root
   logger to a `logging.handlers.QueueHandler` backed by a background-thread `QueueListener`
   (`configure_root_logger=True` by default). Two consequences, both live on the request path:
   (a) the app's own `logging.basicConfig(...)` call in `lifespan()` became a **silent no-op**
   (root already had a handler; `basicConfig` refuses to touch an already-configured root
   logger) — confirmed empirically: log lines were coming out in Litestar's default format,
   not the app's own `"%(asctime)s %(name)s %(levelname)s %(message)s"`; (b) **every** log
   record — including httpx's own `"HTTP Request: POST ..."` INFO line, emitted once per
   upstream call, entirely unrequested — was routed through a cross-thread queue on every
   single request. cProfile on an in-process ASGI harness (AsyncTestClient + ASGI-mock
   upstream, C=32, 4000 requests, warm) traced `QueueListener.dequeue` alone to ~1.76s of
   ~7.39s profiled time (~24%), plus lock acquire/release (~0.63s) and LogRecord
   construction/writes feeding it — all reproducing 1:1 with request count. Fix:
   `logging_config=None` on the `Litestar`/`_LTEngineApp` constructor (so the app's own
   `basicConfig` is the only logging config in effect — plain synchronous `StreamHandler`, no
   queue/thread), plus `logging.getLogger("httpx").setLevel(logging.WARNING)` right after it.
   **Correction (review Minor-7):** httpx's `_client.py` calls `logger.info(...)` at exactly
   two call sites and has no WARNING/ERROR logging of its own at all — a transport failure
   (connect refused, timeout, ...) surfaces as a raised exception, handled by
   `LLMClient`/`errors.map_llm_failure`, not as a log record. So `WARNING` doesn't "keep
   failures visible on a quieter level" (the original comment's claim) — it silences httpx's
   logger **entirely**; there is no failure-path record on this logger to preserve. The code
   comment has been corrected to say this plainly. Re-profiling the same harness after the fix:
   total function calls 11.9M → 10.7M, profiled time 7.39s → 6.43s (~13% down), and
   `dequeue`/lock-contention entirely dropped out of the top 25. **Real-world B1 effect:**
   C=32 gap ~15–18ms → ~7–13ms in repeat runs immediately after this one change (RPS ratio
   also improved, ~0.80 → ~0.87–0.91 avg). **This is the one change in the set with an
   unambiguous, reproducible, profiled effect** (see attribution note below).

2. **`--workers N` on the Python launch** (`backend/src/ltengine/__main__.py`,
   `bench/bench_overhead.py`). The Rust reference's `main.rs` never calls `.workers(N)` on its
   `actix_web::HttpServer` — actix-web's default is one OS worker thread per CPU core (20 here
   — genuine multi-core parallelism for its per-request CPU-bound work). `ltengine.__main__`
   hardcoded Granian to `workers=1` — a single Python **process**, hence a single GIL. Added an
   opt-in `--workers` CLI flag (`default=1`, every existing caller — differential/live tests,
   plain CLI use — unaffected byte-for-byte) and wired B1's py launch to `--workers
   {os.cpu_count()}` (overridable via `LTENGINE_BENCH_WORKERS`).
   **Attribution correction (review Important-3):** the original version of this report called
   this "the single biggest lever." Revisiting the actual repeated-run numbers does not
   support that as a clean, isolated claim — across the 5 post-optimization runs recorded
   below, the C=32 gap ranged 7.13–15.87ms with an RPS ratio range of 0.61–0.97, a spread
   comparable in size to the improvement itself, and rust's own p50 (which `--workers` cannot
   affect at all) varied 29.23–35.64ms across those same 5 runs — i.e. host-level noise alone
   is large enough to explain swings of the same magnitude attributed to `--workers`. The
   change is kept because it is correct in principle (real per-core parallelism vs. a
   GIL-bound single process is a genuine, defensible fix for a concurrency benchmark) and
   does not measurably hurt, but it is **not** claimed as a proven win the way the logging fix
   is — that attribution was overstated. **`--workers > 1` also breaks `/metrics`** (see the
   dedicated note near the end of this report) — it is used here purely as a throughput lever
   for the benchmark, not as a deployment recommendation.

3. **Eliminated the redundant double-JSON-parse on the common (well-typed) request**
   (`backend/src/ltengine/server/routes.py`). `_parse_translate_payload` already produces
   `parsed` via `json.loads(raw)`; the type-check pass then re-walked the **same** JSON text a
   second time via `errors.field_spans()` — unconditionally, on every request, even when every
   field is already correctly typed (the overwhelming majority case). Added a cheap
   `isinstance`-only pre-check (`_has_type_mismatch`) over the already-parsed `dict`; the
   expensive span-tracking re-parse now runs only when a mismatch is actually found, to build
   the precise span-based error message. Purely additive/skip-only — verified against every
   type-mismatch test in the differential suite, no diff. (Fix-wave follow-up: the per-field
   rule that used to be written out twice — once in the fast check, once in the precise loop —
   is now a single shared `_field_type_mismatch(key, value)` predicate both call sites use, so
   the two passes can no longer drift apart; review Minor-6.)

Each change was re-validated against the **full non-live differential suite** (149/0/0) before
moving to the next.

### Results under the old (retired) gate — historical, 5 post-optimization runs

| run | C=32 py p50 | C=32 rust p50 | gap (py−rust) | C=32 RPS ratio (py/rust) |
|---|---|---|---|---|
| 1 | 44.63 | 35.64 | 8.99 ms | 0.97 |
| 2 | 45.10 | 29.23 | 15.87 ms | 0.61 (below 0.70 — the one run that would have also failed the RPS gate) |
| 3 | 41.22 | 34.09 | 7.13 ms | 0.88 |
| 4 | 45.87 | 33.99 | 11.88 ms | 0.92 |
| 5 | 42.73 | 31.81 | 10.92 ms | 0.88 |
| **avg** | | | **10.96 ms** | **0.85** |

C=1/C=8 gaps stayed under the 3ms bar in all 5 runs (same pattern as baseline). Rust's own
C=32 p50 ranged **29.23–35.64ms** across these 5 identical-code repeats (correcting an earlier
draft's "31.8–36.3ms," which mixed in a value from an unrelated `--workers=4` sweep run that
wasn't part of this 5-run set — review Minor-12).

**Under the old gate this was DONE_WITH_CONCERNS**: C=1/C=8 passed; C=32 RPS ratio passed 4/5
runs (avg 0.85); C=32 p50-overhead gap dropped 15.72ms → 10.96ms (30% reduction from the three
optimizations above) but never closed under the 3ms bar. This is exactly the finding the
review used to argue the C=32 p50 bar was unphysical — see "Amended gate" above.

---

## B1 — fresh results (amended gate, current/authoritative)

Same servers, same three optimizations in place, plus the fix-wave harness changes: randomized
arm order per round, per-arm warmup, `--runs 5` median-of-runs at C=32. Three independent
invocations of `uv run --project backend python bench/bench_overhead.py`, no arguments (so
`--runs` defaults to 5):

### Run A (full detail)

**C=1 — GATED: py p50 ≤ rust p50 + 3.0ms**

| arm | p50 ms | p95 ms | p99 ms | RPS |
|---|---|---|---|---|
| direct-mock | 0.71 | 0.90 | 1.03 | 1360 |
| rust | 0.81 | 1.10 | 1.31 | 1179 |
| py | 1.42 | 1.74 | 1.96 | 680 |

C=1 gate: rust p50=0.81ms, py p50=1.42ms, bar=3.81ms → **PASS**

**C=8 — informational only, no gate**

| arm | p50 ms | p95 ms | p99 ms | RPS |
|---|---|---|---|---|
| direct-mock | 7.44 | 16.58 | 25.12 | 901 |
| rust | 6.23 | 15.05 | 22.64 | 1053 |
| py | 7.43 | 10.28 | 12.99 | 1029 |

**C=32 — GATED: median RPS ratio ≥ 0.70 across 5 runs (p50/p95/p99 informational only)**

| run | arm | p50 ms | p95 ms | p99 ms | RPS |
|---|---|---|---|---|---|
| 1 | direct-mock | 29.58 | 111.61 | 162.07 | 759 |
| 1 | rust | 27.61 | 109.31 | 158.93 | 786 |
| 1 | py | 39.11 | 53.21 | 80.64 | 745 |
| 2 | direct-mock | 28.72 | 111.02 | 171.80 | 766 |
| 2 | rust | 26.28 | 105.41 | 153.26 | 830 |
| 2 | py | 33.61 | 127.19 | 204.40 | 671 |
| 3 | direct-mock | 31.86 | 129.82 | 209.71 | 688 |
| 3 | rust | 27.08 | 102.62 | 155.34 | 820 |
| 3 | py | 37.79 | 54.68 | 69.56 | 774 |
| 4 | direct-mock | 34.80 | 122.76 | 193.48 | 660 |
| 4 | rust | 26.22 | 102.89 | 155.37 | 850 |
| 4 | py | 37.69 | 44.44 | 80.01 | 770 |
| 5 | direct-mock | 41.60 | 151.39 | 214.13 | 564 |
| 5 | rust | 28.52 | 109.24 | 176.86 | 779 |
| 5 | py | 34.28 | 140.76 | 202.43 | 632 |

C=32 median RPS: rust=820, py=745, **ratio=0.91** (gate ≥0.70) → **PASS**
C=32 median p50 (informational, not gated): rust=27.08ms, py=37.69ms

**Run A verdict: PASS** (wall time: 65s)

### Runs B and C (summary — full per-run tables omitted for brevity, same structure as Run A)

| run | C=1 gate | C=32 median RPS ratio | verdict |
|---|---|---|---|
| B | PASS | 0.89 | PASS |
| C | PASS | 0.84 | PASS |

All three runs: **B1 verdict: PASS** under the amended gate. RPS ratio stayed comfortably
above the 0.70 floor in every run (0.84–0.91), a much tighter, more reproducible spread than
the old gate's C=32 p50-gap numbers ever showed — exactly what the amendment predicted:
RPS-ratio-of-medians is a more physical, lower-noise statistic at C=32 than a single-run
absolute p50 comparison against a saturating floor.

## B1 — final verdict: **PASS**

C=1 gated bar: PASS (3/3 runs). C=32 median-RPS-ratio gate: PASS (3/3 runs, ratios 0.84–0.91,
all ≥0.70). No FAIL token from either C=8 or the C=32 p50/p95/p99 rows — both are printed as
informational only, per the amended gate.

---

## B2 — end-to-end sanity vs. a real vLLM upstream

Not gated (spec: "sanity, not gated on absolutes" — model latency dominates). 20 texts,
`source=auto`, `target=en`, C=4.

### Confound 1 — engine cold-start ordering (found and fixed, prior investigation)

The original `b2()` launches rust and py against the **same shared vLLM engine** and measures
them sequentially. The first arm in that loop pays the engine's cold-start cost (prefix-cache
priming for the shared system prompt, any CUDA warm-up) that the second arm does not. An
unfixed run showed a large, suspicious gap (rust p50 737ms / p99 3300ms vs. py p50 384ms / p99
481ms); a diagnostic run with the arm order swapped flipped the pattern exactly, confirming an
ordering confound, not a language difference. Fix: `_warm_up_engine()` fires warm-up calls at
the real upstream before either arm is launched or measured.

### Confound 2 — prefix-cache contamination across arms (review Important-1, fixed this wave)

Even with the engine-level warm-up above, the original code built `bodies` (the 20
salted texts) **once**, before the `for name, srv in [("rust", ...), ("py", ...)]` loop, and
reused the identical salted strings for both arms. That let rust's calls warm vLLM's
automatic prefix cache for those exact prompt strings; py's later requests, sending
byte-identical text, could then hit a partially-warm cache rust itself never benefited from —
a second, sneakier ordering confound layered on top of the first (this one about the
per-request TEXT suffix, not the shared system-prompt prefix `_warm_up_engine` already
covers). Fix: `bodies` is now rebuilt with fresh salts **inside** the per-arm loop, so rust and
py never see byte-identical prompt text.

### Confound-fix follow-up — warm-up now uses the real prompt shape (review Minor-13)

`_warm_up_engine`'s payload previously used a generic placeholder message
(`"Translate: hello"`) instead of ltengine's actual system+user prompt shape. It now builds
the warm-up prompt through the same `PromptBuilder` the `/translate` handler itself uses
(`source=auto`, `target=English`, matching B2's real request shape), so the shared
system-prompt prefix vLLM's prefix cache keys off is primed byte-for-byte identically to what
the timed requests actually send.

### Fresh results (both fixes applied) — 2 runs

| arm | p50 ms | p95 ms | p99 ms | RPS |
|---|---|---|---|---|
| rust | 409 | 749 | 761 | 8.62 |
| py | 382 | 500 | 513 | 10.46 |

| arm | p50 ms | p95 ms | p99 ms | RPS |
|---|---|---|---|---|
| rust | 390 | 681 | 681 | 9.40 |
| py | 381 | 533 | 552 | 10.55 |

p50s track within ~5% (rust 390–409ms, py 381–382ms) — consistent with the model dominating,
as expected. A smaller residual tail difference remains (rust p95/p99 still meaningfully
above py's) — plausibly further residual ordering effect (rust is still always measured
first in the loop even though both now start from an equally warm engine and never share
exact prompt text) compounded by the inherent noise of a 20-request/C=4 sample (p95/p99 are
the 1–2 slowest of 20 requests; a single slow generation shifts them substantially). Given B2
is explicitly non-gated and p50 (the statistic that's actually stable at this sample size)
agrees closely between arms, this was not chased further.

### B2 summary: informational PASS (model dominates; no gate to fail). Two confounds found, diagnosed, and fixed (engine cold-start ordering + per-arm prefix-cache contamination); warm-up now uses the real prompt shape.

---

## Live sanity tests

`tests/live/test_live_sanity.py` (`@pytest.mark.live`, excluded by default via
`addopts = "-m 'not live'"` — confirmed: `149 passed, 2 deselected`, the 2 being these tests).

Run: `uv run --project backend pytest tests/live -m live -v` (vLLM engine confirmed idle
immediately before):

```
tests/live/test_live_sanity.py::test_live_translate_both_servers PASSED  [ 50%]
tests/live/test_live_sanity.py::test_live_long_text_heartbeat_py PASSED  [100%]

======================== 2 passed in 131.35s (0:02:11) =========================
```

Teardown in both tests now matches `conftest.py`'s pattern (`terminate()` → `wait(timeout=10)`
→ `kill()` on timeout) via a small local `_terminate()` helper, same as `bench_overhead.py`'s.

---

## `--workers N` breaks `/metrics` (review Important-5)

`ltengine.__main__`'s `--workers` flag (used by B1's py launch, default 1 everywhere else) is
a genuine throughput lever for a concurrency benchmark, but **do not use `N>1` in any
deployment that scrapes `/metrics`**: each Granian worker is a separate OS process with its
own `CollectorRegistry` (built fresh, per-process, inside `create_app()`), so a scrape lands on
whichever worker happened to answer that particular request. Counters are not aggregated
across workers — a client polling `/metrics` sees per-process values that jump around
depending on which worker served the last few requests and are not monotonically increasing
for the service as a whole, breaking Prometheus `rate()`/counter semantics outright. Fine for
a benchmark that ignores `/metrics` entirely (as this one does); a real multi-process
deployment needs a shared/aggregated registry (out of scope here — see the flag's own
`--help` text for the same warning, and the flag's docstring note in `__main__.py`).

---

## Verification

- Full non-live suite before any change: `149 passed` (baseline).
- Full non-live suite after each of the 3 original B1 optimizations (individually, in order):
  `149 passed` each time — no parity regression introduced.
- Full non-live suite after this fix wave's `routes.py` change (`_field_type_mismatch`
  extraction):

  ```
  ........................................................................ [ 48%]
  ........................................................................ [ 96%]
  .....                                                                    [100%]
  149 passed, 2 deselected, 2 warnings in 134.30s (0:02:14)
  ```

  Same 149/0/0; the 2 deselected are the live tests (excluded by `not live` addopts, as
  intended).
- `ruff check` / `ruff format --check`: clean on every touched file
  (`backend/src/ltengine/__main__.py`, `backend/src/ltengine/server/app.py`,
  `backend/src/ltengine/server/routes.py`, `bench/*.py`, `tests/live/*.py`). `__main__.py`
  carries 3 pre-existing `ruff format` findings (confirmed identical against `git stash` from
  before any of this work — unrelated to these edits, not touched).
- `mypy` (project config, `files = ["backend/src"]`): same 3 pre-existing errors as baseline
  (`__main__.py`'s Granian `log_level` argument typing, `app.py`'s `ltengine_frontend` missing
  stub) — no new errors. `bench/` and `tests/live/` fall outside the configured `mypy` scope
  (same as the rest of `tests/`).
