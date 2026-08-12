"""Unit tests for /metrics endpoint."""

from __future__ import annotations

import re

from litestar.testing import TestClient
from ltengine.config import Config
from ltengine.server.app import create_app


def test_metrics_endpoint_present() -> None:
    app = create_app(Config(llm_url="http://127.0.0.1:1", llm_model="m"))
    with TestClient(app=app) as client:
        client.get("/languages")
        r = client.get("/metrics")
        assert r.status_code == 200
        assert r.headers["content-type"].startswith("text/plain")
        # No doubled charset (Minor 6): CONTENT_TYPE_LATEST already carries
        # its own "; charset=utf-8" -- it must appear exactly once.
        assert r.headers["content-type"].count("charset=") == 1
        # Final-review IMPORTANT 5: metric name matches the oracle's
        # actix_web_prom schema (`ltengine_http_requests_total`), not the
        # prior ad hoc `ltengine_requests_total`.
        assert "ltengine_http_requests_total" in r.text
        assert "ltengine_http_requests_duration_seconds" in r.text


def _counter_value(text: str, *, method: str, path: str, status: str) -> float | None:
    """Read the current value of one `ltengine_http_requests_total` series
    from a `/metrics` scrape body. Label order matches the Counter's
    declared label names (endpoint, method, status) -- `path` here is a
    (local) label VALUE, not a label NAME: `endpoint` replaced `path` as the
    label name in the final-review IMPORTANT 5 schema adoption, oracle-
    verified to match actix_web_prom exactly."""
    m = re.search(
        rf'ltengine_http_requests_total\{{endpoint="{re.escape(path)}",'
        rf'method="{re.escape(method)}",status="{re.escape(status)}"\}} ([0-9.]+)',
        text,
    )
    return float(m.group(1)) if m else None


def _client() -> TestClient:
    return TestClient(app=create_app(Config(llm_url="http://127.0.0.1:1", llm_model="m")))


def test_unknown_path_404_is_counted_with_bounded_sentinel_label() -> None:
    # Routing-level 404s (no matched route) never reach any per-route
    # middleware -- Task 10 flagged them as invisible to
    # ltengine_requests_total. The raw path must NEVER become a label value
    # (unbounded cardinality, attacker-controlled); a fixed sentinel is used
    # instead.
    raw_path = "/this-definitely-does-not-exist-abc123.txt"
    with _client() as client:
        r = client.get(raw_path)
        assert r.status_code == 404

        metrics_text = client.get("/metrics").text
        assert _counter_value(metrics_text, method="GET", path="__unmatched__", status="404") == 1.0
        assert raw_path not in metrics_text


def test_wrong_method_405_is_counted_with_bounded_sentinel_label() -> None:
    # /languages is GET-only; POSTing to it 405s at the routing layer, same
    # blind spot as the 404 case above.
    with _client() as client:
        r = client.post("/languages")
        assert r.status_code == 405

        metrics_text = client.get("/metrics").text
        assert (
            _counter_value(metrics_text, method="POST", path="__unmatched__", status="405") == 1.0
        )


def test_translate_is_counted_exactly_once() -> None:
    # Guards against double-counting once static-serving + metrics share one
    # outer ASGI layer: a normal API request must still be recorded exactly
    # once, not twice (once per mechanism). source == target takes the
    # identity fast path in translate() -- no LLM call needed.
    with _client() as client:
        r = client.post("/translate", json={"q": "hello", "source": "en", "target": "en"})
        assert r.status_code == 200

        metrics_text = client.get("/metrics").text
        assert _counter_value(metrics_text, method="POST", path="/translate", status="200") == 1.0
