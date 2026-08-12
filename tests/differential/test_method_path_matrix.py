"""Final-review guard: a {method} x {path} MATRIX, pinning CRITICAL 1
(trailing-slash API paths) and CRITICAL 2 (GET/HEAD on POST-only routes)
across the shapes that matter, not just the one-off cases the earlier
targeted tests covered.

Cells:
  - "/translate", "/translate/"  -- POST-only exact route + its trailing-slash twin
  - "/languages", "/languages/"  -- GET-only exact route + its trailing-slash twin
  - "/health"                    -- GET-only exact route whose GET body is a genuine
                                     empty match on both sides (unlike /index.html below,
                                     no skip needed)
  - "/index.html"                -- a real static asset on BOTH sides (CRITICAL 1/2
                                     don't touch static resolution, but the grid is only
                                     complete with a genuine static hit in it)
  - "/no/such/path"               -- a plain unmatched path (already covered elsewhere;
                                     included here for grid completeness)

Skipped cells (documented, not silently omitted):
  - GET "/index.html": the two servers bundle DIFFERENT UIs (see
    tests/unit/test_static_files.py's module docstring) -- the exact bytes can never
    match. Status + content-type + header-presence are compared instead; body is not.
    Every OTHER method/path combination in this matrix compares full byte-for-byte parity
    via `assert_parity`, header-presence included.
"""

from __future__ import annotations

import pytest

from .helpers import assert_parity, fire_both

B = {"q": "Hello", "source": "en", "target": "ru"}

_PATHS = [
    "/translate",
    "/translate/",
    "/languages",
    "/languages/",
    "/health",
    "/index.html",
    "/no/such/path",
]
_METHODS = ["GET", "HEAD", "POST", "DELETE"]


def _cases() -> list[tuple[str, str]]:
    return [(method, path) for path in _PATHS for method in _METHODS]


@pytest.mark.parametrize(
    "method,path", _cases(), ids=[f"{m}_{p}" for m, p in _cases()]
)
def test_method_path_matrix(pair, method, path) -> None:
    pair.reset()
    kw: dict = {}
    if method == "POST" and path in ("/translate", "/translate/"):
        kw["json_body"] = B
    rust, py = fire_both(pair, method, path, **kw)

    if method == "GET" and path == "/index.html":
        # Documented skip: real static asset, different bundled UI per side --
        # bytes legitimately differ. Everything else about the response
        # (status, content-type, header presence) must still match.
        assert py.status == rust.status == 200
        assert py.content_type == rust.content_type == "text/html"
        assert py.content_type_present and rust.content_type_present
        return

    assert_parity(rust, py)
