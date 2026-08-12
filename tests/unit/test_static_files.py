"""Unit coverage for the outer static-file layer added in Task 11.

The Rust oracle's embedded UI ships different asset names than ours, so real
static-file serving can't be pinned by the differential harness (only the
shared semantics — root `/` -> 200 text/html, an unknown path -> the Rust-
matching 404 body — are differentially tested, in
tests/differential/test_static_parity.py). These tests exercise OUR actual
files directly via Litestar's TestClient.
"""

from __future__ import annotations

from litestar.testing import TestClient
from ltengine.app import create_app
from ltengine.config import Config
from ltengine_frontend import get_static_dir


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
        # Task 6's 404 handler sets an explicit empty content-type — no
        # header value at all, matching Rust's bare `HttpResponse::NotFound()`.
        assert r.headers.get("content-type", "") == ""


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
