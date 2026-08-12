"""CLI entry point for ltengine.

Parses command-line arguments (mirroring LTEngine's --features api flags),
builds a Config, then starts a Granian ASGI server.
"""

from __future__ import annotations

import argparse
import os


def _build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="ltengine",
        description="LibreTranslate-compatible translation server backed by an OpenAI-compatible LLM.",
    )
    p.add_argument(
        "--llm-url",
        required=True,
        metavar="URL",
        help="OpenAI-compatible API base URL (e.g. http://localhost:8000)",
    )
    p.add_argument(
        "--llm-model",
        default="",
        metavar="MODEL",
        help="Model name to use (auto-resolved from /v1/models if empty)",
    )
    p.add_argument(
        "--llm-api-key",
        default="",
        metavar="KEY",
        help="API key for the LLM server",
    )
    p.add_argument(
        "--host",
        default="0.0.0.0",
        metavar="HOST",
        help="Hostname to bind to (default: 0.0.0.0)",
    )
    p.add_argument(
        "--port", "-p",
        type=int,
        default=5050,
        metavar="PORT",
        help="Port to bind to (default: 5050)",
    )
    p.add_argument(
        "--char-limit",
        type=int,
        default=5000,
        metavar="N",
        help="Max characters per translation request (default: 5000)",
    )
    p.add_argument(
        "--api-key",
        default="",
        metavar="KEY",
        help="Optional API key to protect this server",
    )
    p.add_argument(
        "--llm-max-tokens",
        type=int,
        default=0,
        metavar="LLM_MAX_TOKENS",
        help="Maximum output tokens per LLM request (0 = no limit) (default: 0)",
    )
    p.add_argument(
        "--llm-chars-per-token",
        type=float,
        default=0.0,
        metavar="LLM_CHARS_PER_TOKEN",
        help="Dynamic cap (opt-in): characters-per-token divisor. The cap is active only "
             "when this, --llm-max-tokens, and --llm-max-tokens-mult are all > 0 (default: 0)",
    )
    p.add_argument(
        "--llm-max-tokens-mult",
        type=float,
        default=0.0,
        metavar="LLM_MAX_TOKENS_MULT",
        help="Dynamic cap (opt-in): output safety multiple. The cap is active only when "
             "this, --llm-max-tokens, and --llm-chars-per-token are all > 0 (default: 0)",
    )
    p.add_argument(
        "--llm-max-tokens-floor",
        type=int,
        default=64,
        metavar="LLM_MAX_TOKENS_FLOOR",
        help="Dynamic cap: minimum output tokens for tiny inputs (default: 64)",
    )
    p.add_argument(
        "--llm-timeout",
        type=float,
        default=120.0,
        metavar="LLM_TIMEOUT",
        help="HTTP timeout in seconds for LLM API requests (0 = no timeout) (default: 120)",
    )
    p.add_argument(
        "--verbose", "-v",
        action="store_true",
        help="Enable verbose/debug logging",
    )
    p.add_argument(
        "--workers",
        type=int,
        default=1,
        metavar="N",
        help=(
            "Granian worker processes (default: 1). The Rust reference binary's actix-web "
            "runtime defaults to one OS thread per CPU core with no --workers flag at all; a "
            "single Python worker process is GIL-bound to one core no matter how many "
            "concurrent requests arrive, so this is the Python-side lever for the same "
            "real-parallelism headroom (see bench/REPORT.md, B1 optimizations). Left at 1 for "
            "every existing caller (differential/live tests, default CLI use) — raise it "
            "explicitly for a concurrency benchmark or a real multi-core deployment. "
            "WARNING: N>1 currently BREAKS /metrics — each worker is a separate process with "
            "its own CollectorRegistry (built fresh per-process in create_app()), so scrapes "
            "land on whichever worker answered that request and counters are not aggregated "
            "across workers; a client polling /metrics sees per-process values that jump "
            "around and are not monotonically increasing for the service as a whole, breaking "
            "Prometheus rate()/counter semantics. Fine for a throughput benchmark that ignores "
            "/metrics (as bench/bench_overhead.py does); do not use N>1 in any deployment that "
            "scrapes metrics until a shared/aggregated registry exists."
        ),
    )
    return p


def main() -> None:
    parser = _build_parser()
    args = parser.parse_args()

    from .config import Config

    config = Config(
        llm_url=args.llm_url,
        host=args.host,
        port=args.port,
        char_limit=args.char_limit,
        api_key=args.api_key,
        llm_api_key=args.llm_api_key,
        llm_model=args.llm_model,
        llm_max_tokens=args.llm_max_tokens,
        llm_chars_per_token=args.llm_chars_per_token,
        llm_max_tokens_mult=args.llm_max_tokens_mult,
        llm_max_tokens_floor=args.llm_max_tokens_floor,
        llm_timeout=args.llm_timeout,
        verbose=args.verbose,
    )

    print(f"Starting ltengine on http://{config.host}:{config.port}")
    print(f"LLM backend: {config.llm_url}")
    if config.llm_model:
        print(f"Model: {config.llm_model}")

    from granian import Granian
    from granian.constants import Interfaces

    # Granian spawns worker processes via `multiprocessing`. On Windows (always) and
    # macOS (default since Python 3.8) the start method is 'spawn', not 'fork': the
    # worker process is a fresh interpreter that must *import* its target rather than
    # receive a pickled one, because a live Litestar app (its `lifespan` closure, etc.)
    # is not picklable — passing `target=<app instance>` only works on 'fork' platforms,
    # where the child inherits the parent's memory instead of unpickling. So we use
    # Granian's documented factory-import pattern: an import-string target
    # ("ltengine.server.app:app_factory") + factory=True, and hand the config to the worker via
    # environment variables (Config.from_env(), read inside app_factory()) instead of a
    # live object.
    os.environ["LTENGINE_LLM_URL"] = config.llm_url
    os.environ["LTENGINE_HOST"] = config.host
    os.environ["LTENGINE_PORT"] = str(config.port)
    os.environ["LTENGINE_CHAR_LIMIT"] = str(config.char_limit)
    os.environ["LTENGINE_API_KEY"] = config.api_key
    os.environ["LTENGINE_LLM_API_KEY"] = config.llm_api_key
    os.environ["LTENGINE_LLM_MODEL"] = config.llm_model
    os.environ["LTENGINE_LLM_MAX_TOKENS"] = str(config.llm_max_tokens)
    os.environ["LTENGINE_LLM_CHARS_PER_TOKEN"] = str(config.llm_chars_per_token)
    os.environ["LTENGINE_LLM_MAX_TOKENS_MULT"] = str(config.llm_max_tokens_mult)
    os.environ["LTENGINE_LLM_MAX_TOKENS_FLOOR"] = str(config.llm_max_tokens_floor)
    os.environ["LTENGINE_LLM_TIMEOUT"] = str(config.llm_timeout)
    os.environ["LTENGINE_VERBOSE"] = "1" if config.verbose else "0"

    Granian(
        target="ltengine.server.app:app_factory",
        factory=True,
        address=config.host,
        port=config.port,
        interface=Interfaces.ASGI,
        workers=args.workers,
        log_level="debug" if config.verbose else "info",
    ).serve()


if __name__ == "__main__":
    main()
