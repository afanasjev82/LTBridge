"""Unit coverage for app.py's Litestar application factory.

Covers lifespan-constructed resources the differential harness can't observe —
e.g. shared HTTP client config the mock upstream can't reveal (redirect
following: the mock never emits a 3xx, so there's no way to provoke a
follow/don't-follow difference through the wire).
"""

from __future__ import annotations

from litestar.testing import TestClient
from ltengine.app import create_app
from ltengine.config import Config


def test_shared_http_client_matches_reqwest_redirect_defaults() -> None:
    # IMPORTANT fix-wave-2 finding: reqwest follows redirects by default (up to
    # 10); httpx's default is follow_redirects=False. Matching reqwest's
    # ceiling for parity. Not differentially testable — the mock upstream
    # (tests/differential/mock_upstream.py) has no redirect-emitting mode — so
    # this is reasoned from reqwest's documented defaults and asserted directly
    # on the constructed client. See .superpowers/sdd/task-6-report.md, Fix
    # wave 2.
    config = Config(llm_url="http://127.0.0.1:1", llm_model="test-model")
    app = create_app(config)
    with TestClient(app=app) as client:
        http_client = client.app.state.http_client
        assert http_client.follow_redirects is True
        assert http_client.max_redirects == 10
