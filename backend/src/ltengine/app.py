"""Litestar application implementing the LibreTranslate-compatible API.

Port of LTEngine's main.rs (--features api), translated to Python/Litestar.
"""

from __future__ import annotations

import logging
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

import httpx
from litestar import Litestar, Request, Response, get, post, route
from litestar.datastructures import State
from litestar.enums import HttpMethod
from litestar.response import File
from litestar.status_codes import HTTP_200_OK, HTTP_400_BAD_REQUEST, HTTP_403_FORBIDDEN, HTTP_501_NOT_IMPLEMENTED
from litestar.types import ASGIApp, Scope, Receive, Send

from ltengine_frontend import get_static_dir

from .config import Config
from .languages import LANGUAGES, detect_language, get_language_by_code
from .llm_client import LLMClient
from .prompt import PromptBuilder

logger = logging.getLogger(__name__)

_PUNCTUATION = set("!?.,;。")

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _improve_formatting(source: str, translation: str) -> str:
    """Post-process translation output to align punctuation and case with source.

    Direct port of improve_formatting() in LTEngine's main.rs.
    """
    t = translation.strip()

    if not source:
        return ""
    if not t:
        return source

    q_last = source[-1]
    t_last = t[-1]

    if q_last in _PUNCTUATION:
        if q_last != t_last:
            if t_last in _PUNCTUATION:
                t = t[:-1]
            t += q_last
    elif t_last in _PUNCTUATION:
        t = t[:-1]

    if source.islower():
        t = t.lower()
    elif source.isupper():
        t = t.upper()
    elif source and t:
        if source[0].islower() and t[0].isupper():
            t = t[0].lower() + t[1:]
        elif source[0].isupper() and t[0].islower():
            t = t[0].upper() + t[1:]

    return t.strip()


def _validate_format(fmt: str) -> str | None:
    """Return an error message if fmt is not supported, else None."""
    if fmt not in ("text", "html"):
        return "Invalid format. Supported formats: text, html"
    return None


# ---------------------------------------------------------------------------
# Application factory
# ---------------------------------------------------------------------------


def create_app(config: Config) -> Litestar:
    """Build and return the Litestar application for the given config."""

    llm_client: LLMClient | None = None
    http_client: httpx.AsyncClient | None = None
    static_dir: Path = get_static_dir()

    @asynccontextmanager
    async def lifespan(app: Litestar):
        nonlocal llm_client, http_client

        logging.basicConfig(
            level=logging.DEBUG if config.verbose else logging.INFO,
            format="%(asctime)s %(name)s %(levelname)s %(message)s",
        )

        http_client = httpx.AsyncClient(timeout=120.0)
        llm_client = LLMClient(
            url=config.llm_url,
            model=config.llm_model,
            api_key=config.llm_api_key,
            http_client=http_client,
        )

        if not config.llm_model:
            logger.info("Resolving model from %s/v1/models …", config.llm_url)
            resolved = await llm_client.resolve_model()
            logger.info("Using model: %s", resolved)
        else:
            logger.info("Using model: %s", config.llm_model)

        yield

        await http_client.aclose()

    # ------------------------------------------------------------------
    # Route handlers
    # ------------------------------------------------------------------

    @get("/health", name="health")
    async def health() -> Response:
        return Response(content=None, status_code=HTTP_200_OK)

    @get("/languages", name="languages")
    async def languages() -> list[dict[str, Any]]:
        return [
            {"code": lang.code, "name": lang.name, "targets": lang.targets}
            for lang in LANGUAGES
        ]

    @get("/frontend/settings", name="frontend_settings")
    async def frontend_settings() -> dict[str, Any]:
        return {
            "apiKeys": False,
            "charLimit": config.char_limit,
            "filesTranslation": False,
            "frontendTimeout": 1000,
            "keyRequired": bool(config.api_key),
            "language": {
                "source": {"code": "auto", "name": "Auto Detect"},
                "target": {"code": "en", "name": "English"},
            },
            "suggestions": False,
            "supportedFilesFormat": [],
        }

    @post("/detect", name="detect")
    async def detect(request: Request[Any, Any, State]) -> Response:
        body = await request.json()
        q: str | None = body.get("q") if isinstance(body, dict) else None
        if not q or not q.strip():
            return Response(
                content={"error": "Invalid request: missing q parameter"},
                status_code=HTTP_400_BAD_REQUEST,
                media_type="application/json",
            )
        result = detect_language(q)
        return Response(
            content=[{"language": result.language.code, "confidence": result.confidence}],
            media_type="application/json",
        )

    @post("/translate", name="translate")
    async def translate(request: Request[Any, Any, State]) -> Response:
        # Parse JSON or form body
        content_type = request.content_type[0] if request.content_type else ""
        if "application/json" in content_type:
            body = await request.json()
        elif "application/x-www-form-urlencoded" in content_type or "multipart/form-data" in content_type:
            form = await request.form()
            body = dict(form)
        else:
            body = await request.json()

        if not isinstance(body, dict):
            return Response(
                content={"error": "Invalid request body"},
                status_code=HTTP_400_BAD_REQUEST,
                media_type="application/json",
            )

        q: str = body.get("q") or ""
        source: str = body.get("source") or ""
        target: str = body.get("target") or ""
        fmt: str = body.get("format") or "text"
        req_api_key: str = body.get("api_key") or ""
        alternatives: int = int(body.get("alternatives") or 0)

        # Validate required fields
        for field_name, value in [("q", q), ("source", source), ("target", target)]:
            if not value.strip():
                return Response(
                    content={"error": f"Invalid request: missing {field_name} parameter"},
                    status_code=HTTP_400_BAD_REQUEST,
                    media_type="application/json",
                )

        # API key check
        if config.api_key and req_api_key != config.api_key:
            return Response(
                content={"error": "Invalid API key"},
                status_code=HTTP_403_FORBIDDEN,
                media_type="application/json",
            )

        # Char limit
        if len(q) > config.char_limit:
            return Response(
                content={"error": f"Invalid request: request ({len(q)}) exceeds text limit ({config.char_limit})"},
                status_code=HTTP_400_BAD_REQUEST,
                media_type="application/json",
            )

        # Format check
        fmt_error = _validate_format(fmt)
        if fmt_error:
            return Response(
                content={"error": fmt_error},
                status_code=HTTP_400_BAD_REQUEST,
                media_type="application/json",
            )

        # Build prompt
        pb = PromptBuilder()
        pb.set_format(fmt)

        if source == "auto":
            pb.set_source_language("auto")
        else:
            src_lang = get_language_by_code(source)
            if src_lang is None:
                return Response(
                    content={"error": f"{source} is not supported"},
                    status_code=HTTP_400_BAD_REQUEST,
                    media_type="application/json",
                )
            pb.set_source_language(src_lang.name)

        tgt_lang = get_language_by_code(target)
        if tgt_lang is None:
            return Response(
                content={"error": f"{target} is not supported"},
                status_code=HTTP_400_BAD_REQUEST,
                media_type="application/json",
            )
        pb.set_target_language(tgt_lang.name)

        # Translate (skip LLM call if source == target)
        if source != target:
            prompt = pb.build(q)
            try:
                raw = await llm_client.translate(prompt.system, prompt.user)
            except Exception as exc:
                logger.error("LLM translation failed: %s", exc)
                raw = q
            translated_text = _improve_formatting(q, raw)
        else:
            translated_text = q

        result: dict[str, Any] = {"translatedText": translated_text}

        if alternatives > 0:
            result["alternatives"] = []

        if source == "auto":
            detected = detect_language(q)
            result["detectedLanguage"] = {
                "language": detected.language.code,
                "confidence": detected.confidence,
            }

        return Response(content=result, media_type="application/json")

    @post("/translate_file", name="translate_file")
    async def translate_file() -> Response:
        return Response(
            content={"error": "Not implemented"},
            status_code=HTTP_501_NOT_IMPLEMENTED,
            media_type="application/json",
        )

    @post("/suggest", name="suggest")
    async def suggest() -> Response:
        return Response(
            content={"error": "Not implemented"},
            status_code=HTTP_501_NOT_IMPLEMENTED,
            media_type="application/json",
        )

    # ------------------------------------------------------------------
    # Static file middleware (serves frontend UI)
    # ------------------------------------------------------------------

    class StaticFilesMiddleware:
        """Serve static files for any path not matched by an API route."""

        def __init__(self, app: ASGIApp) -> None:
            self.app = app

        async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
            if scope["type"] != "http":
                await self.app(scope, receive, send)
                return

            path: str = scope.get("path", "/")
            # API routes handled by Litestar — only catch root and unknown paths
            api_prefixes = (
                "/translate", "/detect", "/languages", "/health",
                "/frontend", "/suggest",
            )
            if path == "/" or not any(path.startswith(p) for p in api_prefixes):
                # Serve index.html for root; for everything else try the file
                if path == "/":
                    file_path = static_dir / "index.html"
                else:
                    candidate = static_dir / path.lstrip("/")
                    file_path = candidate if candidate.exists() else static_dir / "index.html"

                if file_path.exists():
                    content = file_path.read_bytes()
                    mime = _guess_mime(file_path.suffix)
                    response = Response(
                        content=content,
                        media_type=mime,
                        status_code=200,
                    )
                    await response(scope, receive, send)
                    return

            await self.app(scope, receive, send)

    def _guess_mime(suffix: str) -> str:
        return {
            ".html": "text/html",
            ".js": "application/javascript",
            ".css": "text/css",
            ".json": "application/json",
            ".png": "image/png",
            ".ico": "image/x-icon",
            ".svg": "image/svg+xml",
        }.get(suffix.lower(), "application/octet-stream")

    return Litestar(
        route_handlers=[
            health,
            languages,
            frontend_settings,
            detect,
            translate,
            translate_file,
            suggest,
        ],
        middleware=[StaticFilesMiddleware],
        lifespan=[lifespan],
        debug=config.verbose,
    )
