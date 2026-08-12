"""Build the Rust LTEngine api-mode reference binary in a dedicated worktree.

Never touches the main LTEngine working tree's checked-out branch.
Idempotent: safe to re-run; prints the binary path on success.

Cross-platform (Windows native + WSL/Linux, the canonical build environment):
the LTEngine repo path and the `.rust-ref` worktree's git usability differ by
platform.

- Windows: `.rust-ref` is a normal worktree (gitdir points at a Windows path
  under the main LTEngine repo's `.git/worktrees/`) - checkout/pull/rev-parse
  all work directly against it, as before.
- Linux/WSL: a worktree's gitdir pointer must be Linux-format to be usable by
  git. A `.rust-ref` worktree created *on Windows* has a gitdir pointer file
  containing a `W:/...` path, which git cannot resolve under Linux -
  `git -C .rust-ref ...` fails with "not a git repository". The old behavior
  tolerated that on an existing worktree and asked the *main* LTEngine repo
  to resolve REF_BRANCH's tip instead of the worktree's own HEAD - but the
  main repo's tip moves independently of a worktree that checkout/pull can
  never actually refresh (both silently no-op on the broken pointer), so the
  printed "reference:" hash could quietly diverge from what was really
  built. Instead we self-heal: prune the broken worktree registration and
  recreate it via WSL git, producing Linux-native pointers so checkout/pull/
  rev-parse all work for real from then on, and the printed hash is always
  read from the worktree itself. A tolerated-failure fallback remains only
  for when self-heal itself cannot run; it marks its output as stale rather
  than pretending to be current.
"""

from __future__ import annotations

import os
import platform
import re
import shutil
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
_WINDOWS_PATH_RE = re.compile(r"^[A-Za-z]:[\\/]")


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
    """Short commit hash of the worktree's own checked-out HEAD.

    Always asked of REF_DIR itself, never the main repo: the printed
    reference must be the commit that was actually built, not the main
    repo's live branch tip, which can move independently of a worktree that
    was never actually refreshed.
    """
    out = subprocess.run(
        ["git", "-C", str(REF_DIR), "rev-parse", "--short", "HEAD"],
        capture_output=True,
        text=True,
        check=False,
    )
    if out.returncode == 0:
        return out.stdout.strip()
    print(f"WARNING: could not resolve the worktree's own HEAD: {out.stderr.strip()}")
    return "unknown"


def _worktree_usable() -> bool:
    """True if `git -C REF_DIR ...` genuinely works (gitdir pointer is native)."""
    result = subprocess.run(
        ["git", "-C", str(REF_DIR), "rev-parse", "--is-inside-work-tree"],
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        check=False,
    )
    return result.returncode == 0 and result.stdout.strip() == "true"


def _gitdir_pointer() -> str | None:
    """Raw target of REF_DIR/.git ('gitdir: <path>'), or None if unreadable."""
    git_file = REF_DIR / ".git"
    if not git_file.is_file():
        return None
    text = git_file.read_text(encoding="utf-8", errors="replace").strip()
    prefix = "gitdir:"
    if not text.startswith(prefix):
        return None
    return text[len(prefix) :].strip()


def _self_heal_worktree() -> bool:
    """Recreate REF_DIR as a Linux-native worktree. Returns True on success.

    The existing worktree's gitdir pointer is a Windows path, so neither
    `git worktree remove` (it refuses: the pointer isn't a valid absolute
    path from here) nor plain checkout/pull can touch it. Deleting the
    working directory first makes it unambiguously prunable from any OS,
    then a fresh `worktree add` from WSL git writes Linux-native pointers.
    """
    print(
        f"WARNING: {REF_DIR} is not usable from this OS (Windows-format gitdir "
        "pointer) - self-healing by recreating the worktree via WSL git."
    )
    if REF_DIR.exists():
        shutil.rmtree(REF_DIR)
    try_run(["git", "-C", str(LTENGINE_REPO), "worktree", "prune"])
    ok = try_run(["git", "-C", str(LTENGINE_REPO), "worktree", "add", str(REF_DIR), REF_BRANCH])
    if ok and _worktree_usable():
        print(f"Self-heal OK: {REF_DIR} is now a Linux-native worktree.")
        return True
    print("WARNING: self-heal failed; falling back to a tolerated-failure, stale read.")
    return False


def _checkout_and_pull() -> None:
    run(["git", "-C", str(REF_DIR), "checkout", REF_BRANCH])
    run(["git", "-C", str(REF_DIR), "pull", "--ff-only"])


def main() -> int:
    if not LTENGINE_REPO.exists():
        print(f"ERROR: Rust repo not found at {LTENGINE_REPO}", file=sys.stderr)
        return 1

    stale = False
    if not REF_DIR.exists():
        run(["git", "-C", str(LTENGINE_REPO), "worktree", "add", str(REF_DIR), REF_BRANCH])
    elif IS_WINDOWS:
        pointer = _gitdir_pointer()
        if pointer and not _WINDOWS_PATH_RE.match(pointer):
            print(
                f"ERROR: {REF_DIR} is a WSL-owned worktree (gitdir points at "
                f"'{pointer}', not a Windows path). Run this script under WSL, "
                "or delete .rust-ref and let Windows recreate it.",
                file=sys.stderr,
            )
            return 1
        _checkout_and_pull()
    elif _worktree_usable() or _self_heal_worktree():
        _checkout_and_pull()
    else:
        stale = True

    head = resolve_head()
    if stale:
        print(f"reference: LTEngine@{REF_BRANCH} {head} (STALE - refresh failed)")
    else:
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
