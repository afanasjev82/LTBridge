"""ltengine-frontend: static web UI assets for ltengine."""

from pathlib import Path


def get_static_dir() -> Path:
    """Return the path to the bundled static files directory."""
    return Path(__file__).parent / "static"
