"""ltbridge.server — the Litestar-backed LibreTranslate-compatible API.

Wraps ``ltbridge.core`` (transport-free logic) with the HTTP surface: route
handlers (``routes.create_router``), the application factory
(``app.create_app``), the request boundary (``boundary.boundary``),
static-file serving, Prometheus metrics, and exception handling.

Public embedding surface:

* v0.1.1+ — ``create_router``, ``build_llm_client`` and ``exception_handlers``,
  so a third-party Litestar app (e.g. the smartech-translate proxy) can mount
  LTBridge's routes directly, sharing its own pooled ``httpx.AsyncClient`` and
  reproducing the exact same ``{"error": ...}`` bodies ``create_app`` produces.
* v0.1.2+ — ``boundary`` and ``API_PATHS``. Mounting the router alone is NOT
  enough for parity: the trailing-slash 405, the bundled UI at ``/``, and the
  pinned ``Not found`` 404 all live in an outer ASGI layer that a host must
  wrap around its own app::

      app = boundary(litestar_app, extra_api_paths=frozenset({"/my/route"}))
"""

from .app import build_llm_client, exception_handlers
from .boundary import API_PATHS, boundary
from .routes import create_router

__all__ = [
    "API_PATHS",
    "boundary",
    "build_llm_client",
    "create_router",
    "exception_handlers",
]
