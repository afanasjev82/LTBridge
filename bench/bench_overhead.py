"""B1: wrapper overhead vs the Rust reference on a zero-delay mock upstream.
B2: end-to-end sanity against a real vLLM upstream (model dominates; not gated).

Usage:
  uv run --project backend python bench/bench_overhead.py            # B1 (mock)
  uv run --project backend python bench/bench_overhead.py --b2       # B2 against
      $LTENGINE_BENCH_LLM_URL (default http://localhost:8000)
  uv run --project backend python bench/bench_overhead.py --b2 URL   # B2 against an
      explicit URL

Thresholds (spec §7): py p50 overhead <= rust p50 overhead + 3ms; py RPS >= 70% rust RPS
at C=32.

Timing: wall time is measured with `time.perf_counter()` throughout (monotonic, immune to
the Windows/WSL wall-clock-adjustment quirks that would otherwise skew latency numbers).
B1's two arms (rust, py) are always run back-to-back within the same process invocation so
both see identical machine conditions (background load, thermal state, etc.).
"""

from __future__ import annotations

import argparse
import asyncio
import os
import statistics
import sys
import time
from pathlib import Path

import httpx

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from bench.texts import TEXTS, salted
from tests.differential import conftest as h

CONCURRENCIES = [1, 8, 32]
N_PER_ARM = 2000
BODY = {"q": "Hello, world", "source": "en", "target": "ru"}

# The Rust reference (actix-web, no `.workers(N)` call in main.rs) defaults to one OS
# thread per CPU core — real parallelism for its per-request CPU-bound work. Granian's
# `workers` defaults to 1 in ltengine.__main__ (kept there for every other caller —
# differential/live tests want one predictable process); a single Python worker process
# is GIL-bound to one core regardless of how many requests are concurrently in flight, so
# B1 hands the py arm the same per-core parallelism Rust already gets for free, via
# `--workers`. See bench/REPORT.md, "B1 optimizations".
PY_WORKERS = int(os.environ.get("LTENGINE_BENCH_WORKERS", str(os.cpu_count() or 4)))

# Discovered default for B2 (see task-13-report.md "WSL LLM URL resolution"): vLLM is
# reachable directly at localhost:8000 from inside WSL on this host. Override via
# LTENGINE_BENCH_LLM_URL when that isn't true (e.g. a different WSL networking mode),
# per task instructions — never hardcode the URL into the gated path.
DEFAULT_LLM_URL = os.environ.get("LTENGINE_BENCH_LLM_URL", "http://localhost:8000")


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
    return {"p50_ms": q[49] * 1000, "p95_ms": q[94] * 1000, "p99_ms": q[98] * 1000, "rps": n / wall}


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
            lines.append(
                f"| {c} | {name} | {r['p50_ms']:.2f} | {r['p95_ms']:.2f} "
                f"| {r['p99_ms']:.2f} | {r['rps']:.0f} |"
            )
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
    return {"p50_ms": q[49] * 1000, "p95_ms": q[94] * 1000, "p99_ms": q[98] * 1000, "rps": n / wall}


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
    """
    async with httpx.AsyncClient(timeout=120.0) as client:
        try:
            models = (await client.get(f"{url}/v1/models")).json()
            model = models["data"][0]["id"]
        except (httpx.HTTPError, KeyError, IndexError, ValueError):
            return  # can't discover a model id; skip rather than send a request vLLM would 400
        payload = {
            "model": model,
            "messages": [{"role": "user", "content": "Translate: hello"}],
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
        bodies = [{"q": salted(t), "source": "auto", "target": "en"} for t in TEXTS]
        lines = [
            f"LLM upstream: {real_llm_url}",
            "",
            "| arm | p50 ms | p95 ms | p99 ms | RPS |",
            "|---|---|---|---|---|",
        ]
        for name, srv in [("rust", rust), ("py", py)]:
            r = await run_arm(srv.base_url, len(bodies), 4, bodies=bodies)
            lines.append(
                f"| {name} | {r['p50_ms']:.0f} | {r['p95_ms']:.0f} "
                f"| {r['p99_ms']:.0f} | {r['rps']:.2f} |"
            )
        return lines
    finally:
        for s in (rust, py):
            s.proc.terminate()


async def b1() -> list[str]:
    rust_mock, _, _ = h.start_mock()
    py_mock, _, _ = h.start_mock()
    rust = h._launch_rust(rust_mock.base_url, [])
    py = h._launch_py(py_mock.base_url, ["--workers", str(PY_WORKERS)])
    try:
        return await bench_pair(rust.base_url, py.base_url, rust_mock.base_url, N_PER_ARM)
    finally:
        for s in (rust, py):
            s.proc.terminate()


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument(
        "--b2",
        nargs="?",
        const=DEFAULT_LLM_URL,
        default=None,
        metavar="LLM_URL",
        help="run B2 against a real vLLM URL (default: $LTENGINE_BENCH_LLM_URL or "
        f"{DEFAULT_LLM_URL} when the flag is given with no value)",
    )
    args = ap.parse_args()
    lines = asyncio.run(b2(args.b2)) if args.b2 else asyncio.run(b1())
    print("\n".join(lines))
    return 1 if any("FAIL" in ln for ln in lines) else 0


if __name__ == "__main__":
    sys.exit(main())
