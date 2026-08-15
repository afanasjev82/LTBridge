"""Unit coverage for the outer static-file layer added in Task 11.

The Rust oracle's embedded UI ships different asset names than ours, so real
static-file serving can't be pinned by the differential harness (only the
shared semantics — root `/` -> 200 text/html, an unknown path -> the Rust-
matching 404 body — are differentially tested, in
tests/differential/test_static_parity.py). These tests exercise OUR actual
files directly via Litestar's TestClient.

Fix wave 2 (review findings): canonical (not raw-path) metrics labels for
static hits, HEAD/405 parity on the static surface, and trailing-slash
strictness for the API-path exclusion.
"""

from __future__ import annotations

import re

from litestar.testing import TestClient
from ltbridge.config import Config
from ltbridge.server.app import _resolve_static_path, create_app
from ltbridge_frontend import get_static_dir


def _client() -> TestClient:
    return TestClient(app=create_app(Config(llm_url="http://127.0.0.1:1", llm_model="m")))


def test_app_js_served_with_correct_content_type_and_bytes() -> None:
    static_dir = get_static_dir()
    expected = (static_dir / "app.js").read_bytes()
    with _client() as client:
        r = client.get("/app.js")
        assert r.status_code == 200
        assert r.headers["content-type"].startswith("application/javascript")
        assert r.content == expected


def test_style_css_served_with_correct_content_type_and_bytes() -> None:
    static_dir = get_static_dir()
    expected = (static_dir / "style.css").read_bytes()
    with _client() as client:
        r = client.get("/style.css")
        assert r.status_code == 200
        assert r.headers["content-type"].startswith("text/css")
        assert r.content == expected


def test_unknown_static_path_gets_rust_parity_404() -> None:
    with _client() as client:
        r = client.get("/nope.js")
        assert r.status_code == 404
        assert r.content == b"Not found"
        # Final-review Minor 7 / ledger T6-5: the header must be truly
        # ABSENT, not merely present-with-an-empty-value — the previous
        # `.get("content-type", "") == ""` assertion could not tell the two
        # apart (both read back as ""), which is exactly how the bug hid.
        # Matches Rust's bare `HttpResponse::NotFound()`, which never sets a
        # content-type header at all.
        assert "content-type" not in r.headers


def test_root_serves_index_html_only_at_root() -> None:
    static_dir = get_static_dir()
    expected = (static_dir / "index.html").read_bytes()
    with _client() as client:
        r = client.get("/")
        assert r.status_code == 200
        assert r.headers["content-type"].startswith("text/html")
        assert r.content == expected


def test_unknown_path_no_longer_falls_back_to_index_html() -> None:
    # Regression guard for the exact bug this task fixes: a non-root,
    # non-existent path used to fall back to index.html (200) instead of
    # 404ing — `candidate if candidate.exists() else index.html`.
    static_dir = get_static_dir()
    index_html = (static_dir / "index.html").read_bytes()
    with _client() as client:
        r = client.get("/definitely-not-a-real-asset.xyz")
        assert r.status_code == 404
        assert r.content != index_html


# ---------------------------------------------------------------------------
# Fix wave 2 — CRITICAL: canonical (not raw-path) metrics labels
# ---------------------------------------------------------------------------


def test_resolve_static_path_canonicalizes_alias_forms() -> None:
    # Direct unit coverage of the resolver: every aliasing raw path below
    # must resolve to the SAME canonical label ("/app.js"), not its own raw
    # string — this is what keeps the Prometheus label bounded. Aliases are
    # the reviewer's exact live-verified examples.
    aliases = ["/app.js", "//app.js", "/./app.js", "/app.js/../app.js"]
    canonicals = set()
    for alias in aliases:
        resolved = _resolve_static_path(alias)
        assert resolved is not None, f"{alias!r} should resolve to a real file"
        _file_path, canonical = resolved
        canonicals.add(canonical)
    assert canonicals == {"/app.js"}, canonicals


def test_resolve_static_path_root_is_canonicalized_as_slash() -> None:
    resolved = _resolve_static_path("/")
    assert resolved is not None
    _file_path, canonical = resolved
    assert canonical == "/"


def test_static_hit_metrics_label_collapses_aliases_via_http() -> None:
    # End-to-end version of the above: fire two DIFFERENT raw request paths
    # for the same asset that survive httpx's own client-side URL
    # normalization unmolested (a trailing double slash, and a
    # percent-encoded "." that decodes server-side to a literal "./" segment
    # httpx's normalizer never sees) and assert the /metrics scrape carries
    # exactly ONE label value for that asset — not one per raw alias.
    with _client() as client:
        r1 = client.get("/app.js//")
        r2 = client.get("/%2e/app.js")
        assert r1.status_code == 200
        assert r2.status_code == 200

        metrics_text = client.get("/metrics").text
        labels = set(
            re.findall(
                r'ltengine_http_requests_total\{endpoint="([^"]+)",method="GET",status="200"\}',
                metrics_text,
            )
        )
        app_js_labels = {label for label in labels if "app.js" in label}
        assert app_js_labels == {"/app.js"}, app_js_labels
        # The raw alias strings must never leak into a label of their own.
        assert "/app.js//" not in metrics_text
        assert "/./app.js" not in metrics_text


# ---------------------------------------------------------------------------
# Fix wave 2 — IMPORTANT: HEAD + non-GET parity on the static surface
# ---------------------------------------------------------------------------


def test_head_root_returns_200_empty_body_with_get_content_length() -> None:
    static_dir = get_static_dir()
    expected_length = len((static_dir / "index.html").read_bytes())
    with _client() as client:
        r = client.head("/")
        assert r.status_code == 200
        assert r.content == b""
        assert r.headers["content-length"] == str(expected_length)
        assert r.headers["content-type"].startswith("text/html")


def test_head_asset_returns_200_empty_body_with_get_content_length() -> None:
    static_dir = get_static_dir()
    expected_length = len((static_dir / "app.js").read_bytes())
    with _client() as client:
        r = client.head("/app.js")
        assert r.status_code == 200
        assert r.content == b""
        assert r.headers["content-length"] == str(expected_length)
        assert r.headers["content-type"].startswith("application/javascript")


def test_post_root_gets_oracle_pinned_405() -> None:
    with _client() as client:
        r = client.post("/")
        assert r.status_code == 405
        assert r.content == b"This resource only supports GET and HEAD."
        assert r.headers["allow"] == "GET, HEAD"
        assert r.headers["content-type"].startswith("text/plain")


def test_post_asset_gets_oracle_pinned_405() -> None:
    with _client() as client:
        r = client.post("/app.js")
        assert r.status_code == 405
        assert r.content == b"This resource only supports GET and HEAD."
        assert r.headers["allow"] == "GET, HEAD"


# ---------------------------------------------------------------------------
# Fix wave 2 — Minor: trailing-slash strictness on the API-path exclusion
# ---------------------------------------------------------------------------


def test_trailing_slash_on_api_path_excluded_from_static_resolution() -> None:
    # "/translate/" must not resolve to a static asset. Final-review CRITICAL
    # 1 changed WHY: it is no longer excluded as "an API path with a slash
    # stripped" (that was the actual bug — see
    # test_exact_match_only_excludes_bare_api_path below) — it just isn't a
    # real file on disk either, so this stays None for the ordinary reason.
    assert _resolve_static_path("/translate/") is None
    assert _resolve_static_path("/metrics/") is None


def test_exact_match_only_excludes_bare_api_path() -> None:
    # Final-review CRITICAL 1 regression guard: the OLD exclusion was
    # `path in _API_PATHS or path.rstrip("/") in _API_PATHS`, which made
    # `_resolve_static_path` itself the (wrong) authority on whether
    # "/translate/" was "an API path" — `_LTBridgeApp.__call__` now owns that
    # decision via an exact `path in _API_PATHS` check of its own, and
    # `_resolve_static_path` no longer special-cases the trailing-slash form
    # at all. The exact path is still excluded (never a candidate asset).
    assert _resolve_static_path("/translate") is None
    assert _resolve_static_path("/metrics") is None
    assert _resolve_static_path("/languages") is None


# ---------------------------------------------------------------------------
# Final-review CRITICAL 1/2 — the outer layer OWNS every non-exact path
# ---------------------------------------------------------------------------


def test_post_trailing_slash_on_api_path_gets_pinned_405_not_200() -> None:
    # The exact regression this fix wave closes: Litestar's own router used
    # to slash-normalize "/translate/" onto the "/translate" handler, so a
    # POST there silently 200-translated. It must now 405 like any other
    # non-GET/HEAD miss.
    with _client() as client:
        r = client.post(
            "/translate/", json={"q": "hi", "source": "en", "target": "ru"}
        )
        assert r.status_code == 405
        assert r.content == b"This resource only supports GET and HEAD."
        assert r.headers["allow"] == "GET, HEAD"


def test_get_trailing_slash_on_api_path_gets_pinned_404() -> None:
    with _client() as client:
        r = client.get("/translate/")
        assert r.status_code == 404
        assert r.content == b"Not found"
        assert "content-type" not in r.headers


def test_get_on_post_only_route_gets_404_not_405() -> None:
    # Final-review CRITICAL 2: a GET against a POST-only exact-match route
    # (Litestar raises MethodNotAllowedException) must 404, matching actix's
    # embedded static-file service allowing GET/HEAD through its method
    # guard and then missing on the file lookup — NOT the generic 405.
    with _client() as client:
        r = client.get("/translate")
        assert r.status_code == 404
        assert r.content == b"Not found"
        assert "content-type" not in r.headers


def test_head_on_get_only_route_gets_404_with_empty_body() -> None:
    # Same rule, the GET-only-route/HEAD-method variant, and the one place a
    # HEAD's body must be suppressed on an otherwise-content-bearing 404 —
    # oracle-verified Content-Length stays 9 (`len(b"Not found")`) even
    # though the body itself is empty.
    with _client() as client:
        r = client.head("/languages")
        assert r.status_code == 404
        assert r.content == b""
        assert r.headers["content-length"] == "9"
        assert "content-type" not in r.headers


def test_delete_on_exact_api_path_still_gets_generic_405() -> None:
    # A non-GET/HEAD method against an exact-match route stays a plain 405 —
    # only the GET/HEAD case is redirected to 404 by CRITICAL 2.
    with _client() as client:
        r = client.delete("/translate")
        assert r.status_code == 405
        assert r.content == b"This resource only supports GET and HEAD."
        assert r.headers["allow"] == "GET, HEAD"
