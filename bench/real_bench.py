"""B — real-vLLM bench: time-boxed ABAB throughput/latency comparison, rust vs py, against
the live `smartech-ltengine-vllm` engine (no mock upstream — the model itself is on the
critical path). User-approved protocol (2026-08-15); see the task report appended to
`bench/REPORT.md` under "Real-vLLM benchmark — Rust-free tree (2026-08-15)".

This is INFORMATIONAL, not a gate — see `bench/bench_overhead.py` for the actual B1 mock
gate. Nothing here is imported by, or changes the behavior of, that gated default.

Protocol summary (binding — see the design review that scoped this task):
  - C ladder {1, 2, 4, 8, 16, 32}. Both servers launched with ``--llm-url`` pointed at the
    same real engine (default ``http://localhost:8000``, override via
    ``LTBRIDGE_BENCH_LLM_URL`` — reuses `bench.bench_overhead.DEFAULT_LLM_URL`, never
    hardcoded on the gated path). py runs at the SHIPPED DEFAULT ``--workers`` (1) — no
    `--workers N` override, unlike B1's mock bench.
  - TIME-BOXED cells: each (arm, C) cell runs for `CELL_DURATION_S` (90s) wall-clock,
    maintained-concurrency (C workers each looping request -> record -> next request).
    Only requests that COMPLETE before the cutoff are counted; whatever is in flight at the
    cutoff is cancelled and discarded, not waited for and not counted (`run_arm_timeboxed`).
  - ABAB ordering: the full ladder runs twice. Round 1 is rust-first within every cell-pair
    (rust's 90s window, then py's); round 2 is py-first. Final numbers are the per-(arm, C)
    median across the two rounds (`--assemble`), so neither arm systematically eats a
    first-in-the-pair warm-up/cache tax.
  - Fresh salts per arm per cell (`bench.texts.salted` / `.long_text`) — no two cells, let
    alone the two arms within one cell-pair, ever see byte-identical prompt text.
  - Workload mix: C<=4 draws from the 20 short TEXTS; C>=8 mixes 4 short : 1 long (long ~=
    1-2k chars, synthesized by `bench.texts.long_text`) — see `_bodies_for`.
  - ENGINE-IDLE GATE before EVERY cell (not just every round): poll
    `vllm:num_requests_running` + `vllm:num_requests_waiting` from the engine's `/metrics`
    until both are 0, up to 5 minutes; abort cleanly (raise, never bench a contended engine)
    if it doesn't clear in time.
  - TOKEN-VOLUME FAIRNESS: `vllm:prompt_tokens_total` / `vllm:generation_tokens_total` are
    scraped immediately before and after each cell; the delta is this cell's real token
    volume, cross-checked at `--assemble` time.
  - TURN-SAFETY (hard rule from a prior incident): this module is driven ONE (round, C) CELL
    PAIR PER PROCESS INVOCATION — never the full ladder in one unbounded call, and NEVER
    backgrounded. Each invocation launches its own rust+py servers against the real engine,
    runs its cell pair, tears down, and merges its result into the ``--results`` JSON
    accumulator, so a cell pair can never outlive the foreground call that owns it. Usage:

  # per (round, C) cell pair — 12 calls total for the 6-C ladder x 2 rounds, each running
  # to completion in the FOREGROUND of whatever shell launches it:
  uv run --project backend python -m bench.real_bench --round 1 --c 1  --warmup --results bench/_real_bench.json
  uv run --project backend python -m bench.real_bench --round 1 --c 2  --results bench/_real_bench.json
  ... (round 1, C = 4, 8, 16, 32) ...
  uv run --project backend python -m bench.real_bench --round 2 --c 1  --warmup --results bench/_real_bench.json
  ... (round 2, remaining C's) ...
  uv run --project backend python -m bench.real_bench --assemble --results bench/_real_bench.json --out bench/_real_bench_out.md

``--warmup`` fires the engine warm-up (`bench_overhead._warm_up_engine`, real PromptBuilder
shape) once, ONLY on the first C of a round (per protocol: "before EACH round", not before
every cell) — pass it on exactly the ``--c 1`` invocation of each round.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import re
import statistics
import sys
import time
from pathlib import Path

import httpx

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from bench import bench_overhead as bo
from bench.texts import TEXTS, long_text, salted
from tests.differential import conftest as h

LADDER = [1, 2, 4, 8, 16, 32]
CELL_DURATION_S = 90.0
IDLE_GATE_TIMEOUT_S = 300.0
IDLE_GATE_POLL_S = 3.0

_METRIC_RE_CACHE: dict[str, re.Pattern[str]] = {}


def _bodies_for(c: int) -> list[dict]:
    """Workload mix for concurrency ``c`` (protocol: C<=4 short-only, C>=8 4:1
    short:long). Fresh salts every call — callers must call this once per (arm, C) cell,
    never reuse a list across cells."""
    if c <= 4:
        texts = [salted(t) for t in TEXTS]
    else:
        shorts = [salted(t) for t in TEXTS]
        texts = []
        for i, s in enumerate(shorts):
            texts.append(s)
            if (i + 1) % 4 == 0:
                texts.append(long_text())
    return [{"q": t, "source": "auto", "target": "en"} for t in texts]


def _metric_pattern(name: str) -> re.Pattern[str]:
    if name not in _METRIC_RE_CACHE:
        _METRIC_RE_CACHE[name] = re.compile(
            rf"^{re.escape(name)}(?:\{{[^}}]*\}})?\s+([0-9.eE+\-]+)\s*$", re.MULTILINE
        )
    return _METRIC_RE_CACHE[name]


def _metric_value(metrics_text: str, name: str) -> float:
    """Sum every series' value for a Prometheus counter/gauge ``name`` in raw ``/metrics``
    text (label sets collapsed via summation — safe even if the engine ever reports more
    than one series, e.g. multiple models)."""
    return sum(float(v) for v in _metric_pattern(name).findall(metrics_text))


async def _scrape_metrics(llm_url: str, client: httpx.AsyncClient) -> str:
    r = await client.get(f"{llm_url}/metrics", timeout=10.0)
    r.raise_for_status()
    return r.text


async def wait_for_idle(
    llm_url: str, timeout_s: float = IDLE_GATE_TIMEOUT_S, poll_s: float = IDLE_GATE_POLL_S
) -> None:
    """Poll the real engine's `/metrics` until BOTH `vllm:num_requests_running` and
    `vllm:num_requests_waiting` read 0, up to ``timeout_s``. Raises (never benches a
    contended engine) if it doesn't clear in time — callers should let this propagate and
    abort the run cleanly, per protocol.
    """
    deadline = time.monotonic() + timeout_s
    async with httpx.AsyncClient() as client:
        while True:
            text = await _scrape_metrics(llm_url, client)
            running = _metric_value(text, "vllm:num_requests_running")
            waiting = _metric_value(text, "vllm:num_requests_waiting")
            if running == 0 and waiting == 0:
                return
            if time.monotonic() >= deadline:
                raise RuntimeError(
                    f"ENGINE-IDLE GATE FAILED after {timeout_s:.0f}s: "
                    f"num_requests_running={running} num_requests_waiting={waiting} at "
                    f"{llm_url} — aborting cleanly, refusing to bench a contended engine."
                )
            await asyncio.sleep(poll_s)


async def _token_totals(llm_url: str, client: httpx.AsyncClient) -> tuple[float, float]:
    text = await _scrape_metrics(llm_url, client)
    return (
        _metric_value(text, "vllm:prompt_tokens_total"),
        _metric_value(text, "vllm:generation_tokens_total"),
    )


async def run_arm_timeboxed(
    url: str, duration_s: float, concurrency: int, bodies: list[dict]
) -> dict:
    """Maintained-concurrency, time-boxed load: ``concurrency`` workers each loop
    request -> record -> next request against ``url`` for ``duration_s`` wall-clock
    seconds. Only requests that COMPLETE before the cutoff are counted; `asyncio.wait`'s
    ``timeout`` returns control at the deadline regardless of what's still in flight, and
    the ``pending`` tasks (whichever request each worker was mid-await on) are cancelled —
    discarded, never awaited to completion, never counted. A failed request (non-2xx,
    transport error) is swallowed and retried with the next body; it does not stop the
    worker loop or get counted as a completion.
    """
    latencies: list[float] = []
    started = 0

    async def worker(client: httpx.AsyncClient) -> None:
        nonlocal started
        while True:
            body = bodies[started % len(bodies)]
            started += 1
            t0 = time.perf_counter()
            try:
                r = await client.post(f"{url}/translate", json=body)
                r.raise_for_status()
            except httpx.HTTPError:
                continue
            latencies.append(time.perf_counter() - t0)

    async with httpx.AsyncClient(timeout=120.0) as client:
        tasks = [asyncio.create_task(worker(client)) for _ in range(concurrency)]
        t0 = time.perf_counter()
        _done, pending = await asyncio.wait(tasks, timeout=duration_s)
        wall = time.perf_counter() - t0
        for t in pending:
            t.cancel()
        await asyncio.gather(*pending, return_exceptions=True)

    n = len(latencies)
    if n < 2:
        return {"p50_ms": 0.0, "p95_ms": 0.0, "rps": n / wall if wall else 0.0, "n": n}
    latencies.sort()
    q = statistics.quantiles(latencies, n=100)
    return {"p50_ms": q[49] * 1000, "p95_ms": q[94] * 1000, "rps": n / wall, "n": n}


async def measure_cell(server_url: str, llm_url: str, c: int, duration_s: float) -> dict:
    """One (arm, C) cell: idle-gate -> token totals before -> the timed run -> token
    totals after. Returns the merged result dict (p50/p95/rps/n plus this cell's real
    prompt/generation token deltas)."""
    await wait_for_idle(llm_url)
    async with httpx.AsyncClient() as client:
        prompt_before, gen_before = await _token_totals(llm_url, client)
    bodies = _bodies_for(c)
    result = await run_arm_timeboxed(server_url, duration_s, c, bodies)
    async with httpx.AsyncClient() as client:
        prompt_after, gen_after = await _token_totals(llm_url, client)
    result["prompt_tokens"] = prompt_after - prompt_before
    result["generation_tokens"] = gen_after - gen_before
    return result


async def run_cell_pair(
    round_no: int, c: int, llm_url: str, duration_s: float, do_warmup: bool
) -> dict[str, dict]:
    """Run one cell pair (rust's cell + py's cell, at concurrency ``c``, in the order the
    ABAB protocol dictates for ``round_no``): round 1 is rust-first, round 2 is py-first.
    """
    order = ["rust", "py"] if round_no == 1 else ["py", "rust"]
    if do_warmup:
        print(f"[real-bench] round {round_no}: warming up engine at {llm_url}...", file=sys.stderr)
        await bo._warm_up_engine(llm_url)

    results: dict[str, dict] = {}
    for arm in order:
        print(f"[real-bench] round {round_no} C={c}: launching {arm}...", file=sys.stderr)
        srv = h._launch_rust(llm_url, []) if arm == "rust" else h._launch_py(llm_url, [])
        try:
            print(
                f"[real-bench] round {round_no} C={c} {arm}: idle-gate + "
                f"{duration_s:.0f}s timed cell...",
                file=sys.stderr,
            )
            cell = await measure_cell(srv.base_url, llm_url, c, duration_s)
            results[arm] = cell
            print(
                f"[real-bench] round {round_no} C={c} {arm}: n={cell['n']} "
                f"rps={cell['rps']:.2f} p50={cell['p50_ms']:.0f}ms p95={cell['p95_ms']:.0f}ms "
                f"prompt_tok={cell['prompt_tokens']:.0f} gen_tok={cell['generation_tokens']:.0f}",
                file=sys.stderr,
            )
        finally:
            bo._terminate(srv.proc)
    return results


def _load_results(path: Path) -> dict:
    if not path.exists():
        return {}
    return json.loads(path.read_text(encoding="utf-8"))


def _save_results(path: Path, data: dict) -> None:
    path.write_text(json.dumps(data, indent=2, sort_keys=True), encoding="utf-8")


def _round_table(round_no: int, c: int, results: dict[str, dict]) -> list[str]:
    lines = [
        f"#### Round {round_no}, C={c}",
        "",
        "| arm | n | p50 ms | p95 ms | RPS | prompt tok | gen tok |",
        "|---|---|---|---|---|---|---|",
    ]
    for arm in ("rust", "py"):
        r = results[arm]
        lines.append(
            f"| {arm} | {r['n']} | {r['p50_ms']:.0f} | {r['p95_ms']:.0f} | {r['rps']:.2f} "
            f"| {r['prompt_tokens']:.0f} | {r['generation_tokens']:.0f} |"
        )
    return lines


def _median_over_rounds(data: dict, c: int, arm: str) -> dict[str, float] | None:
    vals = [
        data[str(r)][str(c)][arm]
        for r in (1, 2)
        if str(r) in data and str(c) in data[str(r)] and arm in data[str(r)][str(c)]
    ]
    if not vals:
        return None
    keys = ("p50_ms", "p95_ms", "rps", "n", "prompt_tokens", "generation_tokens")
    return {k: statistics.median(v[k] for v in vals) for k in keys}


def _median_table(cs: list[int], data: dict) -> list[str]:
    lines = [
        "### Median summary per C (median of round 1 + round 2)",
        "",
        "| C | arm | p50 ms | p95 ms | RPS | prompt tok | gen tok |",
        "|---|---|---|---|---|---|---|",
    ]
    for c in cs:
        for arm in ("rust", "py"):
            m = _median_over_rounds(data, c, arm)
            if m is None:
                continue
            lines.append(
                f"| {c} | {arm} | {m['p50_ms']:.0f} | {m['p95_ms']:.0f} | {m['rps']:.2f} "
                f"| {m['prompt_tokens']:.0f} | {m['generation_tokens']:.0f} |"
            )
    return lines


def _fairness_table(cs: list[int], data: dict) -> list[str]:
    """Token-volume fairness cross-check. Raw per-arm TOTAL tokens naturally diverge with
    throughput (whichever arm completes more requests in the same 90s window moves more
    total tokens — that's an expected consequence of RPS, not an unfairness signal by
    itself). The load-bearing fairness number is tokens PER COMPLETED REQUEST (prompt+gen
    tokens / n), which should track the shared `_bodies_for(c)` mix regardless of arm
    throughput; that per-request average is what's flagged at the 10% divergence bar.
    """
    lines = [
        "### Token-volume fairness (prompt+gen tokens per completed request)",
        "",
        (
            "| C | rust tok/req | py tok/req | divergence | flag (>10%) | rust total tok "
            "| py total tok |"
        ),
        "|---|---|---|---|---|---|---|",
    ]
    for c in cs:
        mr = _median_over_rounds(data, c, "rust")
        mp = _median_over_rounds(data, c, "py")
        if mr is None or mp is None:
            continue
        rust_total = mr["prompt_tokens"] + mr["generation_tokens"]
        py_total = mp["prompt_tokens"] + mp["generation_tokens"]
        rust_per_req = rust_total / mr["n"] if mr["n"] else 0.0
        py_per_req = py_total / mp["n"] if mp["n"] else 0.0
        denom = max(rust_per_req, py_per_req) or 1.0
        divergence = abs(rust_per_req - py_per_req) / denom
        flag = "FLAG" if divergence > 0.10 else "ok"
        lines.append(
            f"| {c} | {rust_per_req:.0f} | {py_per_req:.0f} | {divergence:.1%} | {flag} "
            f"| {rust_total:.0f} | {py_total:.0f} |"
        )
    return lines


def _build_report(data: dict) -> list[str]:
    cs = sorted(int(c) for c in {c for r in data.values() for c in r})
    lines: list[str] = []
    for round_no in (1, 2):
        rd = data.get(str(round_no), {})
        for c in sorted(int(c) for c in rd):
            lines.extend(_round_table(round_no, c, rd[str(c)]))
            lines.append("")
    lines.extend(_median_table(cs, data))
    lines.append("")
    lines.extend(_fairness_table(cs, data))
    return lines


async def _run_one(args: argparse.Namespace) -> int:
    results = await run_cell_pair(args.round, args.c, args.llm_url, args.duration, args.warmup)
    if args.results:
        path = Path(args.results)
        merged = _load_results(path)
        merged.setdefault(str(args.round), {})[str(args.c)] = results
        _save_results(path, merged)
        print(f"[real-bench] merged round={args.round} C={args.c} into {path}", file=sys.stderr)
    text = "\n".join(_round_table(args.round, args.c, results))
    print(text)
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    ap.add_argument(
        "--round", type=int, choices=(1, 2), help="ABAB round (1=rust-first, 2=py-first)"
    )
    ap.add_argument("--c", type=int, choices=LADDER, help="concurrency for this cell pair")
    ap.add_argument(
        "--warmup",
        action="store_true",
        help="fire the engine warm-up before this cell pair (pass on exactly the first C "
        "of each round, per protocol 'warm up before EACH round')",
    )
    ap.add_argument(
        "--duration",
        type=float,
        default=CELL_DURATION_S,
        metavar="SECONDS",
        help=f"per-cell time box in seconds (default: {CELL_DURATION_S:.0f})",
    )
    ap.add_argument(
        "--llm-url",
        default=bo.DEFAULT_LLM_URL,
        metavar="URL",
        help=f"real OpenAI-compatible upstream (default: {bo.DEFAULT_LLM_URL}, "
        "$LTBRIDGE_BENCH_LLM_URL)",
    )
    ap.add_argument(
        "--results", metavar="PATH", help="JSON accumulator file to merge this cell pair into"
    )
    ap.add_argument(
        "--assemble",
        action="store_true",
        help="assemble mode: run nothing, load --results and print/--out the full markdown report",
    )
    ap.add_argument("--out", metavar="PATH", help="also write the assembled report to PATH")
    args = ap.parse_args()

    if args.assemble:
        if not args.results:
            ap.error("--assemble requires --results PATH")
        data = _load_results(Path(args.results))
        if not data:
            ap.error(f"no results found in {args.results}")
        text = "\n".join(_build_report(data))
        print(text)
        if args.out:
            Path(args.out).write_text(text + "\n", encoding="utf-8")
        return 0

    if args.round is None or args.c is None:
        ap.error("--round and --c are required for a measurement invocation (or use --assemble)")

    try:
        return asyncio.run(_run_one(args))
    except RuntimeError as e:
        print(f"[real-bench] ABORT: {e}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
