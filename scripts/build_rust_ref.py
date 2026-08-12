"""Build the Rust LTEngine api-mode reference binary in a dedicated worktree.

Never touches the main LTEngine working tree's checked-out branch.
Idempotent: safe to re-run; prints the binary path on success.

Cross-platform (Windows native + WSL/Linux): the LTEngine repo path and the
`.rust-ref` worktree's git usability differ by platform.

- Windows: `.rust-ref` is a normal worktree (gitdir points at a Windows path
  under the main LTEngine repo's `.git/worktrees/`) - checkout/pull/rev-parse
  all work directly against it, as before.
- Linux/WSL: a `.rust-ref` worktree created *on Windows* has a gitdir pointer
  file containing a `W:/...` path, which is not a valid path under Linux, so
  `git -C .rust-ref ...` fails with "not a git repository". When that
  happens on an *existing* worktree, we tolerate it with a warning and carry
  on: the checked-out source tree is a valid pinned snapshot regardless of
  whether git itself can currently introspect it from this OS, and `cargo
  build` only needs the files on disk. To still report which commit is
  pinned, we ask the *main* LTEngine repo (a normal, non-worktree git dir,
  fully usable from either OS) to resolve REF_BRANCH instead of asking the
  worktree to resolve its own HEAD - worktrees share refs/objects with the
  main repo, so `git -C <main repo> rev-parse REF_BRANCH` returns the exact
  commit the worktree has checked out.
"""

from __future__ import annotations

import os
import platform
import subprocess
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
IS_WINDOWS = platform.system() == "Windows"
LTENGINE_REPO = (
    Path("W:/Work/Projects/smartech/LTEngine")
    if IS_WINDOWS
    else Path("/mnt/w/Work/Projects/smartech/LTEngine")
)
REF_DIR = REPO_ROOT / ".rust-ref"
REF_BRANCH = "development"


def run(cmd: list[str], cwd: Path | None = None) -> None:
    print("+", " ".join(cmd))
    subprocess.run(cmd, cwd=cwd, check=True)


def try_run(cmd: list[str], cwd: Path | None = None) -> bool:
    """Like run(), but returns False (with a warning) instead of raising."""
    print("+", " ".join(cmd))
    result = subprocess.run(
        cmd, cwd=cwd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, check=False
    )
    if result.returncode != 0:
        print(
            f"WARNING: command failed (rc={result.returncode}), tolerating: {result.stdout.strip()}"
        )
        return False
    return True


def resolve_head() -> str:
    """Best-effort short commit hash of the worktree's checked-out HEAD."""
    if IS_WINDOWS:
        out = subprocess.run(
            ["git", "-C", str(REF_DIR), "rev-parse", "--short", "HEAD"],
            capture_output=True,
            text=True,
            check=True,
        )
        return out.stdout.strip()
    # Linux: the worktree's own gitdir pointer is a Windows path and won't
    # resolve here. Ask the main repo for REF_BRANCH's tip instead - it
    # shares objects/refs with the worktree, so this is the same commit.
    out = subprocess.run(
        ["git", "-C", str(LTENGINE_REPO), "rev-parse", "--short", REF_BRANCH],
        capture_output=True,
        text=True,
        check=False,
    )
    if out.returncode == 0:
        return out.stdout.strip()
    print(
        f"WARNING: could not resolve HEAD via either the worktree or the main repo: {out.stderr.strip()}"
    )
    return "unknown"


def main() -> int:
    if not LTENGINE_REPO.exists():
        print(f"ERROR: Rust repo not found at {LTENGINE_REPO}", file=sys.stderr)
        return 1
    if not REF_DIR.exists():
        run(["git", "-C", str(LTENGINE_REPO), "worktree", "add", str(REF_DIR), REF_BRANCH])
    elif IS_WINDOWS:
        run(["git", "-C", str(REF_DIR), "checkout", REF_BRANCH])
        run(["git", "-C", str(REF_DIR), "pull", "--ff-only"])
    else:
        # Existing worktree, but its gitdir pointer is a Windows path under
        # WSL - tolerate the failure. The checked-out source is a valid
        # pinned snapshot; cargo only needs the files.
        ok = try_run(["git", "-C", str(REF_DIR), "checkout", REF_BRANCH])
        if ok:
            try_run(["git", "-C", str(REF_DIR), "pull", "--ff-only"])
        else:
            print(
                "WARNING: .rust-ref's worktree git metadata is not usable from this OS "
                "(gitdir points at a Windows path); using the source tree as-is."
            )

    head = resolve_head()
    print(f"reference: LTEngine@{REF_BRANCH} {head}")

    target_dir = (
        Path(os.environ["CARGO_TARGET_DIR"])
        if os.environ.get("CARGO_TARGET_DIR")
        else REF_DIR / "target"
    )
    run(
        [
            "cargo",
            "build",
            "--release",
            "--no-default-features",
            "--features",
            "api",
            "--manifest-path",
            str(REF_DIR / "ltengine" / "Cargo.toml"),
            "--target-dir",
            str(target_dir),
        ]
    )
    exe = "ltengine.exe" if IS_WINDOWS else "ltengine"
    binary = target_dir / "release" / exe
    if not binary.exists():
        print(f"ERROR: expected binary missing: {binary}", file=sys.stderr)
        return 1
    print(f"BINARY: {binary}")
    if target_dir != REF_DIR / "target":
        print(
            f"NOTE: non-default CARGO_TARGET_DIR in use; export LTENGINE_REF_BIN={binary} "
            "before running the differential suite (conftest.py honors it)."
        )
    return 0


if __name__ == "__main__":
    sys.exit(main())
