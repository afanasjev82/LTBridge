"""Extended MOCK-upstream concurrency sweep: Rust vs Python (1 worker vs nproc workers)
vs the raw generator-to-mock floor, across a wide concurrency ladder.

This is a CHARACTERIZATION sweep, not a gate — `bench/bench_overhead.py`'s amended
pass/fail gate (C=1 absolute + C=32 median-ratio) is untouched by this module; nothing
here is imported by, or changes the behavior of, that default `python bench/bench_overhead.py`
invocation.

Usage:
  # One rung per bounded, foreground invocation (production pattern — each call launches its
  # own 4 mocks + 3 servers, runs, tears down, and MERGES into --results; never backgrounded,
  # so a rung can never outlive the process that owns its child servers):
  uv run --project backend python -m bench.sweep --concurrencies 1  --results bench/_sweep.json
  uv run --project backend python -m bench.sweep --concurrencies 2  --results bench/_sweep.json
  ... (one call per C in the ladder) ...
  uv run --project backend python -m bench.sweep --report --results bench/_sweep.json --out bench/_sweep_out.md

  # Or, for a quick smoke test, the whole ladder in one (unbounded) process:
  uv run --project backend python -m bench.sweep --concurrencies 1,2,4

Design (binding requirements from the review that scoped this task):

1. Out-of-process mock. The in-process uvicorn-in-a-thread mock
   (`tests/differential/conftest.py::start_mock`) shares the GIL/event-loop with this same
   process's `httpx` load generator and self-saturates around C=32 (~642rps/35ms on an EMPTY
   body — a property of the harness, not of either server under test). At C=64 it would
   measure itself, not the servers. This sweep spawns the mock as its own OS process via
   `python -m tests.differential.mock_upstream --port N` (the standalone entry point added to
   that module) instead. `start_mock()` itself is untouched — the differential test suite
   keeps using it in-process.
2. Four arms per concurrency:
   - `direct-mock`: generator -> mock directly (the floor/ceiling for this harness+host).
   - `rust`: the Rust reference binary (actix-web, no `--workers` flag -> 1 OS thread/core).
   - `py-w1`: `ltbridge` with the shipped default `--workers 1`.
   - `py-w20`: `ltbridge` with `--workers` = `os.cpu_count()` (matching actix's default
     thread-per-core parallelism; labeled "py-w20" because that's this host's `nproc`).
   Each server arm dispatches to its OWN dedicated mock subprocess (never shared across
   arms), so one arm's load can never perturb another's upstream.
3. Ordering discipline: for whichever concurrencies a given invocation runs (mirrors
   `bench_overhead.py::b1`'s pattern), all 4 arms + all 3 servers + all 4 mocks are launched
   ONCE at the start of THAT invocation and stay alive until it returns — only the load
   generator's target and concurrency change between measurements within a call. Within each
   round, arm order is randomized (`random.shuffle`) so no arm systematically pays a
   first-in-loop warm-up tax, and each arm gets its own untimed warmup batch immediately
   before its timed run (both behaviors reused unchanged from `bench_overhead.run_arm` /
   `run_arm_mock`). Teardown is terminate() -> wait(10) -> kill(), same as
   `bench_overhead._terminate`, and runs in a `finally` so it always fires before the process
   (and therefore the invocation) exits.

   IMPORTANT — this process must run to completion in the FOREGROUND of whatever shell
   launches it. The mock/server subprocesses are children of *this* process; if the caller
   backgrounds this process and then exits/ends its own session before it finishes, the
   child servers are orphaned (still running, no longer supervised) while the sweep's own
   progress is lost. The one-rung-per-invocation pattern in the module Usage above exists
   specifically so each invocation is short enough (single concurrency, bounded wall time)
   to always be run as a plain foreground call.
4. Repeats: C in {1, 2, 4} get 3 rounds; C in {8, 16, 32, 64} get 5 rounds (cheap Cs are
   stable enough at 3; the higher, noisier Cs get the full 5-round median).
5. n per arm per round: 2000 requests at C <= 8, 3000 at C >= 16 (keeps wall time sane while
   still giving the higher-concurrency arms enough samples for a stable p95/p99).
6. Load generator honesty: one asyncio+httpx generator (this process), symmetric across all
   four arms; `direct-mock`'s RPS at a given C IS this generator+mock's ceiling — above
   whatever C it plateaus at, absolute RPS for every arm is generator-bound and only the
   *relative* rust-vs-py comparison stays meaningful (called out again in the printed
   analysis header). httpx client defaults are kept as-is (bench_overhead.py's own history:
   raising keepalive limits measurably made things worse here — not touched).

No pass/fail: this module never exits nonzero on a "bad" number and never prints FAIL/PASS.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import platform
import random
import statistics
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from bench import bench_overhead as bo
from tests.differential import conftest as h

LADDER = [1, 2, 4, 8, 16, 32, 64]
ARMS = ("direct-mock", "rust", "py-w1", "py-w20")
PY_WORKERS_MAX = os.cpu_count() or 20


def rounds_for(c: int) -> int:
    return 3 if c <= 4 else 5


def n_for(c: int) -> int:
    return 2000 if c <= 8 else 3000


# Out-of-process mock spawning (module docstring, point 1) now lives in
# `bench.bench_overhead._launch_mock` / `.MockProc` — B1's default gated mode (re-anchored
# 2026-08-15) needs the identical out-of-process mock this sweep pioneered, so the
# implementation was extracted there instead of duplicated; this module just calls it via
# `bo._launch_mock()` below.


async def _measure_arms(c: int, n: int, urls: dict[str, str]) -> dict[str, dict]:
    """One randomized-order pass over all 4 arms at concurrency `c` (see docstring
    point 3)."""
    arms = [(name, urls[name]) for name in ARMS]
    random.shuffle(arms)
    results: dict[str, dict] = {}
    for name, url in arms:
        results[name] = await (
            bo.run_arm_mock(url, n, c) if name == "direct-mock" else bo.run_arm(url, n, c)
        )
    return results


def _median_arm(round_results: list[dict[str, dict]], arm: str) -> dict[str, float]:
    return {
        stat: statistics.median(r[arm][stat] for r in round_results)
        for stat in ("p50_ms", "p95_ms", "p99_ms", "rps")
    }


def _round_table(c: int, n: int, round_results: list[dict[str, dict]]) -> list[str]:
    lines = [
        f"### C={c} — {len(round_results)} rounds x {n} requests/arm/round",
        "",
        "| round | arm | p50 ms | p95 ms | p99 ms | RPS |",
        "|---|---|---|---|---|---|",
    ]
    for i, res in enumerate(round_results, start=1):
        for arm in ARMS:
            r = res[arm]
            lines.append(
                f"| {i} | {arm} | {r['p50_ms']:.2f} | {r['p95_ms']:.2f} "
                f"| {r['p99_ms']:.2f} | {r['rps']:.0f} |"
            )
    return lines


def _median_table(cs: list[int], medians: dict[int, dict[str, dict[str, float]]]) -> list[str]:
    lines = [
        "### Per-C median summary (all 4 arms)",
        "",
        "| C | arm | p50 ms | p95 ms | p99 ms | RPS |",
        "|---|---|---|---|---|---|",
    ]
    for c in cs:
        for arm in ARMS:
            m = medians[c][arm]
            lines.append(
                f"| {c} | {arm} | {m['p50_ms']:.2f} | {m['p95_ms']:.2f} "
                f"| {m['p99_ms']:.2f} | {m['rps']:.0f} |"
            )
    return lines


def _derived_table(cs: list[int], medians: dict[int, dict[str, dict[str, float]]]) -> list[str]:
    lines = [
        "### Derived: py vs rust gap/ratio, and the generator+mock floor",
        "",
        (
            "| C | rust RPS | py-w1 RPS | py-w1 gap ms (p50) | py-w1/rust ratio | "
            "py-w20 RPS | py-w20 gap ms (p50) | py-w20/rust ratio | floor (direct-mock) RPS |"
        ),
        "|---|---|---|---|---|---|---|---|---|",
    ]
    for c in cs:
        m = medians[c]
        rust, w1, w20, floor = m["rust"], m["py-w1"], m["py-w20"], m["direct-mock"]
        lines.append(
            f"| {c} | {rust['rps']:.0f} | {w1['rps']:.0f} "
            f"| {w1['p50_ms'] - rust['p50_ms']:+.2f} | {w1['rps'] / rust['rps']:.2f} "
            f"| {w20['rps']:.0f} | {w20['p50_ms'] - rust['p50_ms']:+.2f} "
            f"| {w20['rps'] / rust['rps']:.2f} | {floor['rps']:.0f} |"
        )
    return lines


def _env_lines() -> list[str]:
    try:
        oracle_rev = subprocess.run(
            ["git", "-C", str(h.REPO_ROOT / ".rust-ref"), "rev-parse", "--short", "HEAD"],
            capture_output=True,
            text=True,
            check=True,
        ).stdout.strip()
    except (subprocess.CalledProcessError, FileNotFoundError):
        oracle_rev = "unknown"
    return [
        f"- Platform: `{platform.platform()}` under WSL (`wsl.exe -d Ubuntu-20.04`).",
        f"- `nproc` = {os.cpu_count()} -> `py-w20` uses `--workers {PY_WORKERS_MAX}`.",
        f"- Rust oracle (`.rust-ref`) @ `{oracle_rev}`.",
        f"- Python: `{sys.version.split()[0]}` (`{sys.executable}`).",
        (
            "- Mock upstream: OUT-OF-PROCESS (`python -m tests.differential.mock_upstream`), "
            "one dedicated instance per arm — NOT the in-thread `start_mock()` the differential "
            "suite uses (that one self-saturates around C=32 and would measure itself at C=64)."
        ),
        (
            '- Body: `{"q": "Hello, world", "source": "en", "target": "ru"}` — '
            "`source` is explicit, so language DETECTION is never on this path."
        ),
    ]


async def run_sweep(ladder: list[int]) -> tuple[list[str], dict]:
    print(
        f"[sweep] launching 4 mocks + 3 servers (rust, py-w1, py-w{PY_WORKERS_MAX})...",
        file=sys.stderr,
    )
    mocks = {name: bo._launch_mock() for name in ("direct-mock", "rust", "py-w1", "py-w20")}
    rust = h._launch_rust(mocks["rust"].base_url, [])
    pyw1 = h._launch_py(mocks["py-w1"].base_url, ["--workers", "1"])
    pyw20 = h._launch_py(mocks["py-w20"].base_url, ["--workers", str(PY_WORKERS_MAX)])
    urls = {
        "direct-mock": mocks["direct-mock"].base_url,
        "rust": rust.base_url,
        "py-w1": pyw1.base_url,
        "py-w20": pyw20.base_url,
    }
    all_procs = [m.proc for m in mocks.values()] + [rust.proc, pyw1.proc, pyw20.proc]
    raw: dict[int, list[dict[str, dict]]] = {}
    try:
        lines: list[str] = []
        medians: dict[int, dict[str, dict[str, float]]] = {}
        for c in ladder:
            n = n_for(c)
            rounds = rounds_for(c)
            print(f"[sweep] C={c}: {rounds} rounds x {n} req/arm/round...", file=sys.stderr)
            round_results: list[dict[str, dict]] = []
            for r in range(1, rounds + 1):
                res = await _measure_arms(c, n, urls)
                round_results.append(res)
                print(
                    f"[sweep]   round {r}/{rounds}: "
                    + ", ".join(f"{a}={res[a]['rps']:.0f}rps" for a in ARMS),
                    file=sys.stderr,
                )
            raw[c] = round_results
            medians[c] = {arm: _median_arm(round_results, arm) for arm in ARMS}
            lines.extend(_round_table(c, n, round_results))
            lines.append("")
        lines.extend(_median_table(ladder, medians))
        lines.append("")
        lines.extend(_derived_table(ladder, medians))
        return lines, raw
    finally:
        print("[sweep] tearing down...", file=sys.stderr)
        for p in all_procs:
            bo._terminate(p)


def _load_results(path: Path) -> dict[int, list[dict]]:
    if not path.exists():
        return {}
    data = json.loads(path.read_text(encoding="utf-8"))
    return {int(k): v for k, v in data.items()}


def _save_results(path: Path, raw: dict[int, list[dict]]) -> None:
    ordered = {str(c): raw[c] for c in sorted(raw)}
    path.write_text(json.dumps(ordered, indent=2), encoding="utf-8")


def _build_report(raw: dict[int, list[dict]]) -> list[str]:
    """Assemble the full markdown report (per-C round tables + median + derived summary)
    from an already-populated raw-results dict — no server launching, no network calls.
    Used by `--report` to assemble the final report after all rungs have been merged in via
    separate bounded invocations."""
    cs = sorted(raw)
    lines: list[str] = []
    medians: dict[int, dict[str, dict[str, float]]] = {}
    for c in cs:
        round_results = raw[c]
        medians[c] = {arm: _median_arm(round_results, arm) for arm in ARMS}
        lines.extend(_round_table(c, n_for(c), round_results))
        lines.append("")
    lines.extend(_median_table(cs, medians))
    lines.append("")
    lines.extend(_derived_table(cs, medians))
    return lines


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument(
        "--concurrencies",
        default=",".join(str(c) for c in LADDER),
        metavar="C1,C2,...",
        help="comma-separated concurrencies to RUN in THIS invocation (default: full ladder "
        "in one process — pass a single value, e.g. '16', for the bounded one-rung-per-call "
        "production pattern; see module Usage).",
    )
    ap.add_argument(
        "--results",
        metavar="PATH",
        help="JSON accumulator file. This run's raw per-round results are merged into "
        "whatever is already at PATH (existing C's untouched, this run's C's overwritten) "
        "and the merged file is written back — lets multiple bounded invocations build up "
        "one combined result set.",
    )
    ap.add_argument(
        "--report",
        action="store_true",
        help="Assemble mode: run nothing, just load --results and print/--out the full "
        "markdown report (all C's present in the file). Requires --results.",
    )
    ap.add_argument("--out", metavar="PATH", help="also write the markdown report to PATH")
    args = ap.parse_args()

    header = [
        "MOCK-upstream extended concurrency sweep — CHARACTERIZATION ONLY, no pass/fail gate.",
        "",
        *_env_lines(),
        "",
    ]

    if args.report:
        if not args.results:
            ap.error("--report requires --results PATH")
        raw = _load_results(Path(args.results))
        if not raw:
            ap.error(f"no results found in {args.results}")
        text = "\n".join(header + _build_report(raw))
        print(text)
        if args.out:
            Path(args.out).write_text(text + "\n", encoding="utf-8")
        return 0

    ladder = [int(x) for x in args.concurrencies.split(",") if x.strip()]
    lines, raw_run = asyncio.run(run_sweep(ladder))

    if args.results:
        path = Path(args.results)
        merged = _load_results(path)
        merged.update(raw_run)
        _save_results(path, merged)
        print(
            f"[sweep] merged C={ladder} into {path} (now has C={sorted(merged)})", file=sys.stderr
        )

    text = "\n".join(header + lines)
    print(text)
    if args.out:
        Path(args.out).write_text(text + "\n", encoding="utf-8")
    return 0


if __name__ == "__main__":
    sys.exit(main())
