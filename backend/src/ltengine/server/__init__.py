"""ltengine.server — the Litestar-backed LibreTranslate-compatible API.

Wraps ``ltengine.core`` (transport-free logic) with the HTTP surface: route
handlers (``routes.create_router``), the application factory
(``app.create_app``), static-file serving, Prometheus metrics, and exception
handling.
"""
