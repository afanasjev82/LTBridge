"""Unit tests for /metrics endpoint."""

from __future__ import annotations

from litestar.testing import TestClient
from ltengine.app import create_app
from ltengine.config import Config


def test_metrics_endpoint_present() -> None:
    app = create_app(Config(llm_url="http://127.0.0.1:1", llm_model="m"))
    with TestClient(app=app) as client:
        client.get("/languages")
        r = client.get("/metrics")
        assert r.status_code == 200
        assert r.headers["content-type"].startswith("text/plain")
        assert "ltengine_requests_total" in r.text
