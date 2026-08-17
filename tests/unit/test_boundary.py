"""Unit coverage for the EXPORTED request boundary (v0.1.2).

``boundary()`` is the piece an embedder needs and could not get from
``create_router`` alone: mounting the router into a foreign Litestar app
reproduces the response *bodies* but not the non-API-path semantics, because
those live in an outer ASGI layer. Measured on the smartech-translate proxy
before this export existed: ``POST /translate/`` **200-translated** (Litestar
slash-normalizes onto the exact route) where both a standalone LTBridge server
and the Rust oracle 405, and ``GET /`` 404'd where both serve a UI.

These tests wrap a BARE Litestar app — deliberately not ``create_app`` — so
they prove the wrapper carries the semantics on its own, independent of
anything else LTBridge's own factory does. The byte-for-byte equality of the
standalone app with ``boundary(<metrics-observing inner>)`` is guarded
separately, by the differential suite and
``tests/differential/test_method_path_matrix.py``.
"""

from __future__ import annotations

from litestar import Litestar, get, post
from litestar.testing import TestClient
from ltbridge.server import API_PATHS, boundary
from ltbridge.server.app import exception_handlers
from ltbridge_frontend import get_static_dir

_PINNED_405 = b"This resource only supports GET and HEAD."


@post("/translate", name="translate", status_code=200, sync_to_thread=False)
def _translate(data: dict) -> dict:
    return {"translatedText": "inner"}


@get("/proxy/health", name="proxy_health", sync_to_thread=False)
def _proxy_health() -> dict:
    return {"status": "ok"}


def _inner() -> Litestar:
    """A host-style app: LTBridge-shaped route + a route of the host's own."""
    return Litestar(
        route_handlers=[_translate, _proxy_health],
        exception_handlers=exception_handlers(),
        logging_config=None,
    )


def _client(**kwargs) -> TestClient:
    return TestClient(app=boundary(_inner(), **kwargs))


# ---------------------------------------------------------------------------
# Delegation: an exact API path still reaches the inner app
# ---------------------------------------------------------------------------


def test_exact_api_path_is_delegated_to_the_inner_app() -> None:
    with _client() as client:
        r = client.post("/translate", json={"q": "hi"})
        assert r.status_code == 200
        assert r.json() == {"translatedText": "inner"}


# ---------------------------------------------------------------------------
# The regression the export exists for: trailing slash
# ---------------------------------------------------------------------------


def test_post_trailing_slash_gets_the_pinned_405_not_a_translation() -> None:
    with _client() as client:
        r = client.post("/translate/", json={"q": "hi"})
        assert r.status_code == 405
        assert r.content == _PINNED_405
        assert r.headers["allow"] == "GET, HEAD"
        assert r.headers["content-type"].startswith("text/plain")


def test_get_trailing_slash_gets_the_pinned_404() -> None:
    with _client() as client:
        r = client.get("/translate/")
        assert r.status_code == 404
        assert r.content == b"Not found"
        assert "content-type" not in r.headers


# ---------------------------------------------------------------------------
# Static surface, and switching it off
# ---------------------------------------------------------------------------


def test_root_serves_the_bundled_ui_when_serve_static() -> None:
    expected = (get_static_dir() / "index.html").read_bytes()
    with _client() as client:
        r = client.get("/")
        assert r.status_code == 200
        assert r.headers["content-type"].startswith("text/html")
        assert r.content == expected


def test_root_404s_when_serve_static_is_off() -> None:
    with _client(serve_static=False) as client:
        r = client.get("/")
        assert r.status_code == 404
        assert r.content == b"Not found"
        assert "content-type" not in r.headers


def test_real_asset_404s_when_serve_static_is_off() -> None:
    # Not just root: the WHOLE static surface goes away, so a file that
    # genuinely exists on disk must 404 too.
    assert (get_static_dir() / "app.js").is_file()
    with _client(serve_static=False) as client:
        assert client.get("/app.js").status_code == 404


def test_unknown_path_gets_the_pinned_404_with_no_content_type() -> None:
    with _client() as client:
        r = client.get("/nope")
        assert r.status_code == 404
        assert r.content == b"Not found"
        assert "content-type" not in r.headers


def test_head_on_unknown_path_keeps_the_get_content_length() -> None:
    with _client() as client:
        r = client.head("/nope")
        assert r.status_code == 404
        assert r.content == b""
        assert r.headers["content-length"] == "9"


# ---------------------------------------------------------------------------
# extra_api_paths — the host's own routes
# ---------------------------------------------------------------------------


def test_host_route_is_swallowed_without_extra_api_paths() -> None:
    # The failure mode `extra_api_paths` exists to prevent, pinned explicitly:
    # a host route outside the union never reaches the host's handler.
    with _client() as client:
        r = client.get("/proxy/health")
        assert r.status_code == 404
        assert r.content == b"Not found"


def test_extra_api_paths_lets_a_host_route_through() -> None:
    with _client(extra_api_paths=frozenset({"/proxy/health"})) as client:
        r = client.get("/proxy/health")
        assert r.status_code == 200
        assert r.json() == {"status": "ok"}


def test_extra_api_paths_is_exact_match_only() -> None:
    # A trailing-slash variant of a host route is NOT implied — same strictness
    # the built-in set has.
    with _client(extra_api_paths=frozenset({"/proxy/health"})) as client:
        assert client.get("/proxy/health/").status_code == 404


def test_extra_api_paths_widens_the_static_exclusion_too() -> None:
    # A host route must never be shadowed by a same-named file on the static
    # surface: `index.html` IS a real asset, so listing it as a host route has
    # to route to the inner app (a 404 from the bare inner app here), not serve
    # the file.
    assert (get_static_dir() / "index.html").is_file()
    with _client(extra_api_paths=frozenset({"/index.html"})) as client:
        r = client.get("/index.html")
        assert r.status_code == 404  # inner app has no such route
        assert r.content == b"Not found"


# ---------------------------------------------------------------------------
# The exported constant
# ---------------------------------------------------------------------------


def test_api_paths_is_frozen_and_covers_the_public_surface() -> None:
    assert isinstance(API_PATHS, frozenset)
    assert API_PATHS == {
        "/health",
        "/languages",
        "/frontend/settings",
        "/detect",
        "/translate",
        "/translate_file",
        "/suggest",
        "/metrics",
    }


def test_boundary_carries_no_metrics_of_its_own() -> None:
    # The host owns metrics: the wrapper must not register a collector or
    # expose a scrape endpoint of its own. `/metrics` is in API_PATHS purely as
    # a DELEGATION entry (LTBridge's standalone app defines that route); with a
    # bare inner app it must 404, not serve a Prometheus body.
    with _client() as client:
        r = client.get("/metrics")
        assert r.status_code == 404
        assert b"ltengine_http_requests_total" not in r.content
