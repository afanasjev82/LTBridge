"""Build the Rust LTEngine api-mode reference binary in a dedicated worktree.

Never touches the main LTEngine working tree's checked-out branch.
Idempotent: safe to re-run; prints the binary path on success.
"""
from __future__ import annotations

import platform
import subprocess
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
LTENGINE_REPO = Path("W:/Work/Projects/smartech/LTEngine")
REF_DIR = REPO_ROOT / ".rust-ref"
REF_BRANCH = "development"


def run(cmd: list[str], cwd: Path | None = None) -> None:
    print("+", " ".join(cmd))
    subprocess.run(cmd, cwd=cwd, check=True)


def main() -> int:
    if not LTENGINE_REPO.exists():
        print(f"ERROR: Rust repo not found at {LTENGINE_REPO}", file=sys.stderr)
        return 1
    if not REF_DIR.exists():
        run(["git", "-C", str(LTENGINE_REPO), "worktree", "add",
             str(REF_DIR), REF_BRANCH])
    else:
        run(["git", "-C", str(REF_DIR), "checkout", REF_BRANCH])
        run(["git", "-C", str(REF_DIR), "pull", "--ff-only"])
    head = subprocess.run(["git", "-C", str(REF_DIR), "rev-parse", "--short", "HEAD"],
                          capture_output=True, text=True, check=True).stdout.strip()
    print(f"reference: LTEngine@{REF_BRANCH} {head}")
    run(["cargo", "build", "--release", "--no-default-features", "--features", "api",
         "--manifest-path", str(REF_DIR / "ltengine" / "Cargo.toml"),
         "--target-dir", str(REF_DIR / "target")])
    exe = "ltengine.exe" if platform.system() == "Windows" else "ltengine"
    binary = REF_DIR / "target" / "release" / exe
    if not binary.exists():
        print(f"ERROR: expected binary missing: {binary}", file=sys.stderr)
        return 1
    print(f"BINARY: {binary}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
