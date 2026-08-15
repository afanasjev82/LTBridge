"""B1: wrapper overhead vs the Rust reference on a zero-delay mock upstream.
B2: end-to-end sanity against a real vLLM upstream (model dominates; not gated).

Usage:
  uv run --project backend python bench/bench_overhead.py            # B1 (mock)
  uv run --project backend python bench/bench_overhead.py --runs 8   # B1, 8 repeats at C=32
  uv run --project backend python bench/bench_overhead.py --b2       # B2 against
      $LTBRIDGE_BENCH_LLM_URL (default http://localhost:8000)
  uv run --project backend python bench/bench_overhead.py --b2 URL   # B2 against an
      explicit URL

Amended gate (2026-08-12, review + human amendment, commit e5abca1). The original bar
("py p50 overhead <= rust p50 overhead + 3ms" at every C) was shown to be unphysical at
C=32: the in-process mock itself saturates under that much concurrency (~642rps/35ms with
an EMPTY body — not a Python-vs-Rust effect at all), the floor-subtraction that produced
"overhead" cancels out algebraically in the pass/fail comparison either way, and the
resulting bar sat below this benchmark's own run-to-run instrument noise (rust's own C=32
p50 swung ~29-36ms across identical repeats — see bench/REPORT.md). The amended gate:

  - C=1 (single run): GATED — py_p50 <= rust_p50 + 3.0ms. No saturation at C=1, so the
    bar is physical here.
  - C=8 (single run): informational only, no gate (same reasoning trajectory as C=32,
    just not as severe — not worth a second unphysical bar).
  - C=32 (>= 5 repeated runs, `--runs N`): GATED on RATIO OF MEDIAN RPS values —
    median(py_rps) / median(rust_rps) >= 0.60 across the repeats (re-anchored 2026-08-15,
    see below — was 0.70 against the in-process mock). p50/p95/p99 are still printed per
    run, labeled INFORMATIONAL, to keep them visible without gating on them.

Re-anchored 2026-08-15 (user decision; see
docs/superpowers/specs/2026-08-11-ltengine-py-strict-parity-port-design.md §7). The C=32 gate
above was calibrated on the in-process mock (`tests/differential/conftest.py::start_mock()`,
an in-thread uvicorn server sharing this process's GIL/event-loop with the httpx load
generator). That mock self-saturates around C=32 (~642rps/35ms on an EMPTY body — see
bench/REPORT.md), and it throttled rust more than py, inflating the measured ratio
(0.84-0.91 in-process vs ~0.66 honest). The default gated mode now dispatches both the
`direct-mock` floor and each server's own mock to the OUT-OF-PROCESS mock
(`python -m tests.differential.mock_upstream`, the same standalone process `bench/sweep.py`
spawns for its wider ladder — `_launch_mock()` below is shared by both modules so there is
exactly one mock-launching implementation) and the C=32 gate threshold moves 0.70 -> 0.60,
preserving the same relative margin the old bar had over its own (inflated) measured ratio.
The gate run also prints an informational absolute-headroom line comparing the measured py
median RPS against a ~16rps production-ceiling reference (fleet ~= 32 slots at ~2s/request) —
this is never gated, just context for how irrelevant the sub-millisecond per-request deltas
below are once GPU latency dominates.

Timing: wall time is measured with `time.perf_counter()` throughout (monotonic, immune to
the Windows/WSL wall-clock-adjustment quirks that would otherwise skew latency numbers).
B1's arms always run back-to-back within the same process invocation so all three see
identical machine conditions (background load, thermal state, etc.) as closely as a shared,
uncontrolled host allows.

Ordering discipline: the three arms (direct-mock, rust, py) are measured in a randomized
order on every round (every C=1/C=8 run and every one of the C=32 repeats) — whichever arm
happens to run first pays a disproportionate share of connection/JIT/OS-cache warm-up cost
otherwise (this is exactly the confound B2 hit and diagnosed via an order-swap test; see
bench/REPORT.md "B1 ordering discipline" / "B2 prefix-cache confound"). Each arm also gets
a small untimed per-arm warmup batch immediately before its own timed run, so a cold first
connection doesn't land inside the measured latencies.
"""

from __future__ import annotations

import argparse
import asyncio
import os
import random
import statistics
import subprocess
import sys
import time
from dataclasses import dataclass
from pathlib import Path

import httpx

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from ltbridge.core.prompt import PromptBuilder

from bench.texts import TEXTS, salted
from tests.differential import conftest as h

N_PER_ARM = 2000
DEFAULT_RUNS = 5
ARM_WARMUP = 5
BODY = {"q": "Hello, world", "source": "en", "target": "ru"}

# The Rust reference (actix-web, no `.workers(N)` call in main.rs) defaults to one OS
# thread per CPU core — real parallelism for its per-request CPU-bound work. Granian's
# `workers` defaults to 1 in ltbridge.__main__ (kept there for every other caller —
# differential/live tests want one predictable process); a single Python worker process
# is GIL-bound to one core regardless of how many requests are concurrently in flight, so
# B1 hands the py arm the same per-core parallelism Rust already gets for free, via
# `--workers`. NOTE (review Important-3): across repeated runs this was NOT cleanly
# separable from host-level noise — the logging fix (see server/app.py's
# `logging_config=None`) is the one change with an unambiguous, reproducible effect in
# profiling; `--workers` is kept because it is correct in principle (real parallelism vs.
# a GIL-bound single process) and does not measurably hurt, but is not claimed as a proven
# win on its own. See bench/REPORT.md, "B1 optimizations" / "attribution".
PY_WORKERS = int(os.environ.get("LTBRIDGE_BENCH_WORKERS", str(os.cpu_count() or 4)))

# Discovered default for B2 (see task-13-report.md "WSL LLM URL resolution"): vLLM is
# reachable directly at localhost:8000 from inside WSL on this host. Override via
# LTBRIDGE_BENCH_LLM_URL when that isn't true (e.g. a different WSL networking mode),
# per task instructions — never hardcode the URL into the gated path.
DEFAULT_LLM_URL = os.environ.get("LTBRIDGE_BENCH_LLM_URL", "http://localhost:8000")

# Informational-only reference for the C=32 headroom line (see module docstring
# "Re-anchored 2026-08-15"): an approximate production serving ceiling of a 32-slot fleet
# at ~2s/request. Never gated — just makes the sub-millisecond per-request deltas legible
# against what production concurrency/latency actually looks like.
PRODUCTION_CEILING_RPS = 16.0


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


@dataclass
class MockProc:
    base_url: str
    proc: subprocess.Popen


def _launch_mock() -> MockProc:
    """Spawn the standalone OUT-OF-PROCESS mock upstream — its own OS process, not the
    in-thread uvicorn `tests.differential.conftest.start_mock()` the differential suite
    uses. That in-thread mock shares this process's GIL/event-loop with the httpx load
    generator and self-saturates around C=32 (see module docstring "Re-anchored
    2026-08-15"), which is fine for correctness checks but invalidates a throughput
    measurement. This is the ONE mock-launching implementation shared by B1's default
    gated mode (below) and `bench/sweep.py`'s wider characterization ladder — extracted
    here (rather than duplicated) so there is a single place that knows how to spawn it.
    """
    port = h.free_port()
    proc = subprocess.Popen(
        [
            sys.executable,
            "-m",
            "tests.differential.mock_upstream",
            "--host",
            "127.0.0.1",
            "--port",
            str(port),
        ],
        cwd=str(h.REPO_ROOT),
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    base = f"http://127.0.0.1:{port}"
    h._wait_http(f"{base}/v1/models")
    return MockProc(base, proc)


async def run_arm(
    url: str, n: int, concurrency: int, bodies=None, warmup: int = ARM_WARMUP
) -> dict:
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

        if warmup:
            # Untimed, this arm's own connection pool / event loop only — not the
            # cross-arm ordering bias the shuffle in `_measure_arms` addresses.
            warm_sem = asyncio.Semaphore(min(concurrency, 4))

            async def warm(i: int) -> None:
                body = bodies[i % len(bodies)] if bodies else BODY
                async with warm_sem:
                    try:
                        r = await client.post(f"{url}/translate", json=body)
                        r.raise_for_status()
                    except httpx.HTTPError:
                        pass  # best-effort; a failed warm-up call still leaves the timed run valid

            await asyncio.gather(*(warm(i) for i in range(warmup)))

        t0 = time.perf_counter()
        await asyncio.gather(*(one(i) for i in range(n)))
        wall = time.perf_counter() - t0
    latencies.sort()
    q = statistics.quantiles(latencies, n=100)
    return {"p50_ms": q[49] * 1000, "p95_ms": q[94] * 1000, "p99_ms": q[98] * 1000, "rps": n / wall}


async def run_arm_mock(mock_url: str, n: int, concurrency: int, warmup: int = ARM_WARMUP) -> dict:
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

        if warmup:
            warm_sem = asyncio.Semaphore(min(concurrency, 4))

            async def warm() -> None:
                async with warm_sem:
                    try:
                        r = await client.post(f"{mock_url}/v1/chat/completions", json=payload)
                        r.raise_for_status()
                    except httpx.HTTPError:
                        pass

            await asyncio.gather(*(warm() for _ in range(warmup)))

        t0 = time.perf_counter()
        await asyncio.gather(*(one() for _ in range(n)))
        wall = time.perf_counter() - t0
    latencies.sort()
    q = statistics.quantiles(latencies, n=100)
    return {"p50_ms": q[49] * 1000, "p95_ms": q[94] * 1000, "p99_ms": q[98] * 1000, "rps": n / wall}


async def _measure_arms(
    c: int, n: int, mock_url: str, rust_url: str, py_url: str
) -> dict[str, dict]:
    """Measure all three arms (direct-mock, rust, py) at concurrency ``c``, in a
    RANDOMIZED order each call — see the module docstring's "Ordering discipline".
    """
    arms = [("direct-mock", mock_url), ("rust", rust_url), ("py", py_url)]
    random.shuffle(arms)
    results: dict[str, dict] = {}
    for name, url in arms:
        results[name] = await (
            run_arm_mock(url, n, c) if name == "direct-mock" else run_arm(url, n, c)
        )
    return results


def _arm_rows(results: dict[str, dict]) -> list[str]:
    lines = ["| arm | p50 ms | p95 ms | p99 ms | RPS |", "|---|---|---|---|---|"]
    for name in ("direct-mock", "rust", "py"):
        r = results[name]
        lines.append(
            f"| {name} | {r['p50_ms']:.2f} | {r['p95_ms']:.2f} | {r['p99_ms']:.2f} | {r['rps']:.0f} |"
        )
    return lines


async def bench_c1(mock_url: str, rust_url: str, py_url: str, n: int) -> tuple[list[str], bool]:
    """C=1: the only concurrency with an absolute p50-overhead gate (see module
    docstring "Amended gate")."""
    results = await _measure_arms(1, n, mock_url, rust_url, py_url)
    lines = ["### C=1 — GATED: py p50 <= rust p50 + 3.0ms (single run)", "", *_arm_rows(results)]
    rust_p50 = results["rust"]["p50_ms"]
    py_p50 = results["py"]["p50_ms"]
    bar = rust_p50 + 3.0
    ok = py_p50 <= bar
    lines.append("")
    lines.append(
        f"C=1 gate: rust p50={rust_p50:.2f}ms, py p50={py_p50:.2f}ms, "
        f"bar (rust p50 + 3.0ms)={bar:.2f}ms -> {'PASS' if ok else 'FAIL'}"
    )
    if not ok:
        lines.append(f"**FAIL: C=1 py p50 {py_p50:.2f}ms > bar {bar:.2f}ms**")
    return lines, ok


async def bench_c8(mock_url: str, rust_url: str, py_url: str, n: int) -> list[str]:
    """C=8: informational only — no gate (see module docstring "Amended gate")."""
    results = await _measure_arms(8, n, mock_url, rust_url, py_url)
    return ["", "### C=8 — informational only, no gate (single run)", "", *_arm_rows(results)]


async def bench_c32(
    mock_url: str, rust_url: str, py_url: str, n: int, runs: int
) -> tuple[list[str], bool]:
    """C=32: GATED on median(py_rps)/median(rust_rps) >= 0.60 across ``runs`` repeats,
    measured against the OUT-OF-PROCESS mock (see module docstring "Re-anchored
    2026-08-15"; was >= 0.70 against the in-process mock). p50/p95/p99 are printed per
    run but are explicitly INFORMATIONAL — the absolute overhead bar at C=32 is retired
    (see module docstring "Amended gate").
    """
    lines = [
        "",
        (
            f"### C=32 — GATED: median RPS ratio (py/rust) >= 0.60 across {runs} runs. "
            "p50/p95/p99 below are INFORMATIONAL ONLY (not gated)."
        ),
        "",
        "| run | arm | p50 ms | p95 ms | p99 ms | RPS |",
        "|---|---|---|---|---|---|",
    ]
    rust_rps: list[float] = []
    py_rps: list[float] = []
    rust_p50: list[float] = []
    py_p50: list[float] = []
    for run_idx in range(1, runs + 1):
        results = await _measure_arms(32, n, mock_url, rust_url, py_url)
        for name in ("direct-mock", "rust", "py"):
            r = results[name]
            lines.append(
                f"| {run_idx} | {name} | {r['p50_ms']:.2f} | {r['p95_ms']:.2f} "
                f"| {r['p99_ms']:.2f} | {r['rps']:.0f} |"
            )
        rust_rps.append(results["rust"]["rps"])
        py_rps.append(results["py"]["rps"])
        rust_p50.append(results["rust"]["p50_ms"])
        py_p50.append(results["py"]["p50_ms"])
    median_rust_rps = statistics.median(rust_rps)
    median_py_rps = statistics.median(py_rps)
    ratio = median_py_rps / median_rust_rps
    ok = ratio >= 0.60
    lines.append("")
    lines.append(
        f"C=32 median RPS over {runs} runs: rust={median_rust_rps:.0f}, py={median_py_rps:.0f}, "
        f"ratio={ratio:.2f} (gate: >= 0.60) -> {'PASS' if ok else 'FAIL'}"
    )
    if not ok:
        lines.append(f"**FAIL: C=32 median RPS ratio {ratio:.2f} < 0.60**")
    lines.append(
        f"C=32 median p50 (informational, NOT gated): rust={statistics.median(rust_p50):.2f}ms, "
        f"py={statistics.median(py_p50):.2f}ms"
    )
    headroom = median_py_rps / PRODUCTION_CEILING_RPS
    lines.append(
        f"py median RPS = {median_py_rps:.0f} (≈ {headroom:.0f}x the "
        f"~{PRODUCTION_CEILING_RPS:.0f}rps production-ceiling reference)"
    )
    return lines, ok


async def _warm_up_engine(url: str) -> None:
    """Fire a couple of throwaway requests at ``url`` so a fresh vLLM engine's
    cold-start cost (prefix-cache priming for the shared system prompt, CUDA
    warm-up) lands here, not on whichever arm happens to run first below.

    B2 launches rust and py against the SAME shared LLM engine and measures
    them one after the other (``for name, srv in [("rust", ...), ("py",
    ...)]``); without this, the first arm in that loop always eats the
    engine's cold start and the second is measured warm — a real, observed
    effect (see bench/REPORT.md "B2 ordering confound"): a 20-call/C=4 batch
    with no warm-up showed the first arm's p95/p99 several times the
    second's even though p50 tracked closely. Neither `rust` nor `py` is
    used for this — a plain upstream call keeps the warm-up itself out of
    either arm's own measured path.

    The warm-up prompt is built through the SAME `PromptBuilder` ltbridge's own
    `/translate` handler uses (source=auto, target=English — matching B2's real
    request shape), not a generic placeholder message, so the shared system-prompt
    prefix vLLM's automatic prefix caching keys off is primed byte-for-byte
    identically to what the real timed requests will send (review Minor-13).
    """
    prompt = (
        PromptBuilder().set_source_language("auto").set_target_language("English").build("hello")
    )
    async with httpx.AsyncClient(timeout=120.0) as client:
        try:
            models = (await client.get(f"{url}/v1/models")).json()
            model = models["data"][0]["id"]
        except (httpx.HTTPError, KeyError, IndexError, ValueError):
            return  # can't discover a model id; skip rather than send a request vLLM would 400
        payload = {
            "model": model,
            "messages": [
                {"role": "system", "content": prompt.system},
                {"role": "user", "content": prompt.user},
            ],
            "max_tokens": 8,
        }
        for _ in range(2):
            try:
                await client.post(f"{url}/v1/chat/completions", json=payload)
            except httpx.HTTPError:
                pass  # best-effort; a failed warm-up call still leaves the arms comparable


async def b2(real_llm_url: str) -> list[str]:
    print(f"B2: dispatching against LLM upstream {real_llm_url}", file=sys.stderr)
    await _warm_up_engine(real_llm_url)
    rust = h._launch_rust(real_llm_url, [])
    py = h._launch_py(real_llm_url, [])
    try:
        lines = [
            f"LLM upstream: {real_llm_url}",
            "",
            "| arm | p50 ms | p95 ms | p99 ms | RPS |",
            "|---|---|---|---|---|",
        ]
        for name, srv in [("rust", rust), ("py", py)]:
            # Fresh salts PER ARM (review Important-1): building `bodies` once and
            # reusing the identical salted strings for both arms let rust's calls warm
            # vLLM's prefix cache for those EXACT prompts, so py's later identical
            # request text would hit a partially-warm cache rust never got the benefit
            # of — a second, sneakier version of the ordering confound the engine-level
            # warm-up above doesn't cover (that one primes the shared SYSTEM prompt
            # prefix; this one is about the per-request TEXT suffix). Salting inside
            # the loop means the two arms never see byte-identical prompts.
            bodies = [{"q": salted(t), "source": "auto", "target": "en"} for t in TEXTS]
            r = await run_arm(srv.base_url, len(bodies), 4, bodies=bodies, warmup=0)
            lines.append(
                f"| {name} | {r['p50_ms']:.0f} | {r['p95_ms']:.0f} "
                f"| {r['p99_ms']:.0f} | {r['rps']:.2f} |"
            )
        return lines
    finally:
        _terminate(rust.proc)
        _terminate(py.proc)


async def b1(runs: int) -> list[str]:
    # OUT-OF-PROCESS mocks (re-anchored 2026-08-15 — see module docstring): each mock is
    # its own OS process, sharing no GIL/event-loop with the httpx load generator driving
    # it, so it doesn't self-saturate at C=32 the way the differential suite's in-thread
    # `h.start_mock()` does.
    rust_mock = _launch_mock()
    py_mock = _launch_mock()
    rust = h._launch_rust(rust_mock.base_url, [])
    py = h._launch_py(py_mock.base_url, ["--workers", str(PY_WORKERS)])
    try:
        c1_lines, c1_ok = await bench_c1(rust_mock.base_url, rust.base_url, py.base_url, N_PER_ARM)
        c8_lines = await bench_c8(rust_mock.base_url, rust.base_url, py.base_url, N_PER_ARM)
        c32_lines, c32_ok = await bench_c32(
            rust_mock.base_url, rust.base_url, py.base_url, N_PER_ARM, runs
        )
        lines = [*c1_lines, *c8_lines, *c32_lines]
        ok = c1_ok and c32_ok
        lines.append("")
        lines.append(f"**B1 verdict: {'PASS' if ok else 'FAIL'}**")
        return lines
    finally:
        _terminate(rust.proc)
        _terminate(py.proc)
        _terminate(rust_mock.proc)
        _terminate(py_mock.proc)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument(
        "--b2",
        nargs="?",
        const=DEFAULT_LLM_URL,
        default=None,
        metavar="LLM_URL",
        help="run B2 against a real vLLM URL (default: $LTBRIDGE_BENCH_LLM_URL or "
        f"{DEFAULT_LLM_URL} when the flag is given with no value)",
    )
    ap.add_argument(
        "--runs",
        type=int,
        default=DEFAULT_RUNS,
        metavar="N",
        help=f"B1 only: number of C=32 repeats; minimum 5 required for the amended median RPS gate "
        f"(default: {DEFAULT_RUNS}). Ignored for --b2.",
    )
    args = ap.parse_args()
    if args.runs < 5:
        ap.error(
            "--runs must be >= 5 (the amended C=32 gate requires a median over at least 5 runs)"
        )
    lines = asyncio.run(b2(args.b2)) if args.b2 else asyncio.run(b1(args.runs))
    print("\n".join(lines))
    return 1 if any("FAIL" in ln for ln in lines) else 0


if __name__ == "__main__":
    sys.exit(main())
