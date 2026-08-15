"""Startup banner: ASCII art, build info, and the applied-config table.

Python port of LTEngine's ``banner.rs`` + ``startup_info.rs``
(``.rust-ref/ltengine/src/{banner,startup_info}.rs``) — same section layout
(ASCII art -> version line -> Build -> Runtime -> Token budget), rendered as
plain text for the console only. This module never touches the HTTP surface;
the differential suite is the guard that keeps it that way.
"""

from __future__ import annotations

import subprocess
import sys
from importlib import metadata
from pathlib import Path

from ..config import Config

# Same box/figlet convention (slant font, bordered box, "Local AI Machine
# Translation" subtitle) as the Rust reference's raw string (see banner.rs),
# but re-lettered for the LTBridge name — figlet -f slant "LTBridge",
# reboxed to match the border/border-width rule the Rust art used. Trailing
# spaces on several lines (part of the art) are intentional; do not trim.
_ASCII_ART = (
    "\n"
    " _________________________________________\n"
    "    __  __________       _     __         \n"
    "   / / /_  __/ __ )_____(_)___/ /___ ____ \n"
    "  / /   / / / __  / ___/ / __  / __ `/ _ \\\n"
    " / /___/ / / /_/ / /  / / /_/ / /_/ /  __/\n"
    "/_____/_/ /_____/_/  /_/\\__,_/\\__, /\\___/ \n"
    "                             /____/       \n"
    "\n"
    "        Local AI Machine Translation\n"
    "__________________________________________"
)

_GIT_TIMEOUT_S = 2.0


def _git(*args: str) -> str | None:
    """Run ``git <args>`` from this file's directory; return trimmed stdout or ``None``.

    Best-effort exactly like the Rust ``build.rs`` ``git_output`` helper: a
    missing ``git`` binary, an absent ``.git`` (e.g. a wheel install with no
    repo checkout), or any nonzero exit/timeout degrades to ``None`` — never
    raises, so a broken git toolchain can't crash startup.
    """
    try:
        out = subprocess.run(
            ["git", *args],
            cwd=Path(__file__).resolve().parent,
            capture_output=True,
            text=True,
            timeout=_GIT_TIMEOUT_S,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    if out.returncode != 0:
        return None
    text = out.stdout.strip()
    return text or None


def _git_summary() -> str:
    """``{shortsha}{-dirty?} ({branch})`` — mirrors ``build.rs``'s ``LTE_GIT_SHA``/``LTE_GIT_BRANCH``."""
    sha = _git("rev-parse", "--short", "HEAD")
    if sha is None:
        sha = "unknown"
    else:
        status = _git("status", "--porcelain")
        if status:  # non-None AND non-empty output -> dirty tree
            sha = f"{sha}-dirty"
    branch = _git("rev-parse", "--abbrev-ref", "HEAD") or "unknown"
    return f"{sha} ({branch})"


def _pkg_version(name: str, fallback: str = "unknown") -> str:
    try:
        return metadata.version(name)
    except metadata.PackageNotFoundError:
        return fallback


def _secret_state(value: str) -> str:
    """``"set"``/``"none"`` — mirrors Rust's ``secret_state``; never echoes ``value``."""
    return "set" if value.strip() else "none"


def _fmt_num(value: float) -> str:
    """Drop a trailing ``.0`` for whole numbers, matching Rust's f64 ``Display``."""
    return f"{value:g}"


def _render_section(title: str, rows: list[tuple[str, str]]) -> str:
    """Title on its own line, then each row indented and label-padded so all
    values line up — same alignment rule as Rust's ``render_section``."""
    width = max((len(k) for k, _ in rows), default=0)
    lines = [title]
    lines.extend(f"  {k:<{width}}  {v}" for k, v in rows)
    return "\n".join(lines)


def _build_rows() -> list[tuple[str, str]]:
    return [
        ("git", _git_summary()),
        ("granian", _pkg_version("granian")),
        ("litestar", _pkg_version("litestar")),
    ]


def _model_row(config: Config, resolved_model: str | None) -> str:
    if config.llm_model:
        return config.llm_model
    if resolved_model:
        return f"{resolved_model} (resolved)"
    return "(unresolved)"


def _runtime_rows(
    config: Config, resolved_model: str | None, workers: int
) -> list[tuple[str, str]]:
    timeout = f"{_fmt_num(config.llm_timeout)}s" if config.llm_timeout > 0 else "none"
    return [
        ("mode", "api"),
        ("llm url", config.llm_url),
        ("llm model", _model_row(config, resolved_model)),
        ("llm api key", _secret_state(config.llm_api_key)),
        ("server auth", _secret_state(config.api_key)),
        ("bind", f"{config.host}:{config.port}"),
        ("char limit", str(config.char_limit)),
        ("llm timeout", timeout),
        ("workers", str(workers)),  # Python-specific: --workers has no Rust equivalent
    ]


def _token_budget_rows(config: Config) -> tuple[bool, list[tuple[str, str]]]:
    """``(enabled, rows)`` — same gate as ``core.token_budget.output_cap``:
    disabled unless ``mult``, ``chars_per_token``, AND ``llm_max_tokens`` (the
    ceiling) are all > 0."""
    has_ceiling = config.llm_max_tokens > 0
    enabled = config.llm_max_tokens_mult > 0.0 and config.llm_chars_per_token > 0.0 and has_ceiling
    rows = [
        ("chars/token", _fmt_num(config.llm_chars_per_token)),
        ("mult", f"×{_fmt_num(config.llm_max_tokens_mult)}"),
        ("floor", str(config.llm_max_tokens_floor)),
        ("ceiling", str(config.llm_max_tokens) if has_ceiling else "none"),
    ]
    return enabled, rows


def render_banner(config: Config, resolved_model: str | None, workers: int) -> str:
    """Render the full startup banner: ASCII art + version line + Build /
    Runtime / Token budget sections. Console-only — has no effect on the HTTP
    surface. Mirrors ``banner::print_banner`` + ``startup_info::print`` from
    the Rust reference.

    Args:
        config: The resolved server configuration.
        resolved_model: The model name auto-resolved from ``/v1/models`` when
            ``config.llm_model`` is empty, else ``None`` (an explicit
            ``--llm-model`` is shown as-is, with no "(resolved)" suffix).
        workers: Granian worker process count (``--workers``) — a
            Python-specific row with no Rust equivalent.

    Returns:
        The complete banner text, newline-terminated between sections but
        without a trailing newline (the caller's ``print()`` adds one).
    """
    pkg_version = _pkg_version("ltbridge", fallback="0.0.0")
    py_version = f"{sys.version_info.major}.{sys.version_info.minor}.{sys.version_info.micro}"
    title = f"LTBridge {pkg_version} · api · python {py_version}"
    separator = "─" * len(title)

    enabled, budget_rows = _token_budget_rows(config)
    budget_title = f"Token budget  {'ENABLED' if enabled else 'DISABLED'}"

    blocks = [
        _ASCII_ART,
        f"{title}\n{separator}",
        _render_section("Build", _build_rows()),
        _render_section("Runtime", _runtime_rows(config, resolved_model, workers)),
        _render_section(budget_title, budget_rows),
    ]
    return "\n\n".join(blocks)
