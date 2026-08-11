"""CLI entry point for ltengine.

Parses command-line arguments (mirroring LTEngine's --features api flags),
builds a Config, then starts a Granian ASGI server.
"""

from __future__ import annotations

import argparse
import sys


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
        "--verbose", "-v",
        action="store_true",
        help="Enable verbose/debug logging",
    )
    return p


def main() -> None:
    parser = _build_parser()
    args = parser.parse_args()

    from .config import Config
    from .app import create_app

    config = Config(
        llm_url=args.llm_url,
        host=args.host,
        port=args.port,
        char_limit=args.char_limit,
        api_key=args.api_key,
        llm_api_key=args.llm_api_key,
        llm_model=args.llm_model,
        verbose=args.verbose,
    )

    app = create_app(config)

    print(f"Starting ltengine on http://{config.host}:{config.port}")
    print(f"LLM backend: {config.llm_url}")
    if config.llm_model:
        print(f"Model: {config.llm_model}")

    from granian import Granian
    from granian.constants import Interfaces

    Granian(
        target=app,
        address=config.host,
        port=config.port,
        interface=Interfaces.ASGI,
        workers=1,
        log_level="debug" if config.verbose else "info",
    ).serve()


if __name__ == "__main__":
    main()
