"""ltbridge.server — the Litestar-backed LibreTranslate-compatible API.

Wraps ``ltbridge.core`` (transport-free logic) with the HTTP surface: route
handlers (``routes.create_router``), the application factory
(``app.create_app``), static-file serving, Prometheus metrics, and exception
handling.

Public embedding surface (v0.1.1+): ``create_router``, ``build_llm_client``,
and ``exception_handlers`` are re-exported so a third-party Litestar app
(e.g. the smartech-translate proxy) can mount LTBridge's routes directly,
sharing its own pooled ``httpx.AsyncClient`` and reproducing the exact same
``{"error": ...}`` bodies ``create_app`` produces.
"""

from .app import build_llm_client, exception_handlers
from .routes import create_router

__all__ = ["build_llm_client", "create_router", "exception_handlers"]
