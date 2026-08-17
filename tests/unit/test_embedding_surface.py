"""Public embedding surface: exception_handlers(), build_llm_client(), create_router.

An embedder (e.g. the smartech-translate proxy) mounts ``create_router(...)``
into its OWN Litestar app and needs (a) the same three exception handlers
``create_app`` registers, so error bodies stay byte-identical, and (b) a way
to build the ``LLMClient`` sharing the embedder's own pooled ``httpx.AsyncClient``.
"""

from __future__ import annotations

from litestar import Litestar
from litestar.exceptions import MethodNotAllowedException, NotFoundException
from litestar.testing import TestClient
from ltbridge.config import Config
from ltbridge.core.errors import LlmError
from ltbridge.server import build_llm_client, create_router, exception_handlers


def test_exception_handlers_mapping_has_the_three_types() -> None:
    m = exception_handlers()
    assert set(m) == {LlmError, NotFoundException, MethodNotAllowedException}
    assert all(callable(v) for v in m.values())


def test_router_embeds_into_a_foreign_app_with_parity_error_bodies() -> None:
    import httpx

    cfg = Config(llm_url="http://127.0.0.1:1", llm_model="m")
    client = build_llm_client(cfg, httpx.AsyncClient())
    app = Litestar(
        route_handlers=[create_router(cfg, client)],
        exception_handlers=exception_handlers(),
        logging_config=None,
    )
    with TestClient(app=app) as tc:
        r = tc.post("/translate", json={"q": "x", "source": "en", "target": "ru"})
        # unreachable upstream -> LlmError -> {"error": ...} 503 (LTBridge taxonomy)
        assert r.status_code == 503 and set(r.json()) == {"error"}
        assert tc.get("/languages").status_code == 200
