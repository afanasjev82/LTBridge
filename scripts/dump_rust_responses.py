"""Fire error-provoking cases at a manually-started Rust server; print raw responses.

Usage: uv run python scripts/dump_rust_responses.py http://127.0.0.1:PORT MOCK_CTL_URL
Start the pair by hand first (see tests/differential/conftest.py), or run via pytest -s
with a debugger breakpoint; simplest: python -i with the conftest imported.

The exact strings this prints were transcribed into backend/src/ltengine/errors.py and
tests/differential/test_error_parity.py — see .superpowers/sdd/task-6-report.md for the
full captured table (including the unknown-path/405/malformed-JSON/read-timeout cases
this script doesn't cover, captured via a throwaway pytest per the task-6 brief).
"""

from __future__ import annotations

import sys

import httpx

CASES: list[tuple[str, dict]] = [
    ("upstream_502", {"status": 502, "body_mode": "ok"}),
    ("upstream_503", {"status": 503, "body_mode": "ok"}),
    ("upstream_504", {"status": 504, "body_mode": "ok"}),
    ("upstream_429", {"status": 429, "body_mode": "ok"}),
    (
        "upstream_400_with_body",
        {"status": 400, "body_mode": "custom", "custom_body": '{"detail":"boom"}'},
    ),
    ("garbage_json", {"status": 200, "body_mode": "garbage"}),
    ("empty_choices", {"status": 200, "body_mode": "empty_choices"}),
]


def main() -> None:
    server, mock = sys.argv[1], sys.argv[2]
    for name, behavior in CASES:
        httpx.post(
            f"{mock}/__mock__/behavior",
            json={"reply_text": "MOCK_TRANSLATION", "delay_s": 0.0, **behavior},
        )
        r = httpx.post(
            f"{server}/translate", json={"q": "Hello", "source": "en", "target": "ru"}, timeout=30.0
        )
        print(f"--- {name}: HTTP {r.status_code}\n{r.text!r}\n")


if __name__ == "__main__":
    main()
