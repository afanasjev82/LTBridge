"""One command, either OS: ensure the Rust oracle, then run the full suite.

Environment pivot (2026-08-12, see .superpowers/sdd/progress.md): the native
Windows Rust toolchain is broken (missing x64 SDK libs), so the Rust oracle
build AND the differential suite that needs it are WSL-only. (The server
itself is pure Python now — the whatlang PyO3 binding that also forced this
was replaced by ltbridge.core.whatlang — but the oracle still needs cargo.)
This script makes "one command" true from either side:

- On Windows, it re-execs itself inside WSL (``wsl.exe -d Ubuntu-20.04``),
  translating this repo's Windows path to its ``/mnt/<drive>/...`` mount
  point, and simply relays the WSL-side return code.
- Under WSL/Linux, it does the real work: build the Rust reference binary
  if it is missing (via scripts/build_rust_ref.py), then run the suite.

A suite report with ANY skipped tests is NOT a pass. The differential
harness treats a missing/broken Rust oracle as `pytest.skip()`, not a
failure (see tests/differential/conftest.py `_launch_rust`) — this let a
fully-skipped suite exit 0 once already (Task 10 incident, progress.md:
"37 passed, 93 skipped" was reported as green because the Linux oracle
binary had been deleted mid-session). This script parses the pytest
summary line itself and fails loudly on any nonzero skip count instead of
trusting the process return code alone.
"""

from __future__ import annotations

import platform
import re
import shlex
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
WSL_DISTRO = "Ubuntu-20.04"
EXE = "ltengine.exe" if platform.system() == "Windows" else "ltengine"
BIN = ROOT / ".rust-ref" / "target" / "release" / EXE

_SUMMARY_LINE_RE = re.compile(r"^=+.* in [\d.]+s(?: \([^)]*\))? =+\s*$")
_SKIPPED_RE = re.compile(r"(\d+) skipped")


def _to_wsl_path(win_path: Path) -> str:
    """Translate a Windows path (e.g. ``W:\\Work\\...``) to its WSL mount point."""
    drive = win_path.drive.rstrip(":").lower()
    rest = win_path.as_posix()[len(win_path.drive) :]
    return f"/mnt/{drive}{rest}"


def _run_via_wsl() -> int:
    """Re-exec this same script inside WSL and relay its exit code.

    The build/test toolchain lives only in WSL now, so there is nothing
    useful to do on the Windows side beyond handing off — even checking for
    a Windows oracle binary would be checking for something that can no
    longer be built here.
    """
    wsl_root = _to_wsl_path(ROOT)
    inner = f"cd {shlex.quote(wsl_root)} && uv run python scripts/run_suite.py"
    cmd = ["wsl.exe", "-d", WSL_DISTRO, "--", "bash", "-lc", inner]
    print("+ (re-exec via WSL)", " ".join(cmd))
    return subprocess.run(cmd, check=False).returncode


def _find_summary_line(output: str) -> str | None:
    lines = [ln for ln in output.splitlines() if _SUMMARY_LINE_RE.match(ln.strip())]
    return lines[-1] if lines else None


def _skipped_count(summary_line: str) -> int:
    m = _SKIPPED_RE.search(summary_line)
    return int(m.group(1)) if m else 0


def _run_native() -> int:
    if not BIN.exists():
        print(f"Rust reference oracle missing at {BIN}; building...")
        rc = subprocess.run(
            [sys.executable, str(ROOT / "scripts" / "build_rust_ref.py")], check=False
        ).returncode
        if rc != 0:
            print("ERROR: failed to build the Rust reference oracle.", file=sys.stderr)
            return rc

    proc = subprocess.Popen(
        ["uv", "run", "pytest", "tests", "-v"],
        cwd=ROOT,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        bufsize=1,
    )
    lines: list[str] = []
    assert proc.stdout is not None
    for line in proc.stdout:
        print(line, end="")
        lines.append(line)
    proc.wait()
    output = "".join(lines)

    summary = _find_summary_line(output)
    if summary is None:
        print(
            "ERROR: could not find a pytest summary line in the output; "
            "treating this as a failed run.",
            file=sys.stderr,
        )
        return proc.returncode or 1

    skipped = _skipped_count(summary)
    if skipped:
        print(
            f"ERROR: suite reported {skipped} SKIPPED test(s) — this means the Rust "
            f"oracle at {BIN} was missing or broken during the run (the differential "
            "harness skips rather than fails when it can't launch the reference "
            f"binary), NOT a clean pass. Summary: {summary.strip()}",
            file=sys.stderr,
        )
        return 1

    if proc.returncode != 0:
        print(f"SUITE FAILED: {summary.strip()}", file=sys.stderr)
        return proc.returncode

    print(f"SUITE PASSED: {summary.strip()}")
    return 0


def main() -> int:
    if platform.system() == "Windows":
        return _run_via_wsl()
    return _run_native()


if __name__ == "__main__":
    sys.exit(main())
