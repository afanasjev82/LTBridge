"""Async OpenAI-compatible LLM API client.

Port of LTEngine's llm_api.rs — calls an external OpenAI-compatible server
(e.g. vLLM, llama-server, Ollama, OpenAI) to perform translation.
"""

from __future__ import annotations

import httpx

from . import errors


def _is_valid_completion_shape(data: object) -> bool:
    """Return True iff ``data`` matches ``ChatCompletionResponse``'s strict shape.

    Mirrors serde's structural contract for llm_api.rs's
    ``ChatCompletionResponse { choices: Vec<Choice>, usage: Option<Usage> }`` /
    ``Choice { message: ResponseMessage, finish_reason: Option<String> }`` /
    ``ResponseMessage { content: String }``: the body must be a JSON object with
    a ``choices`` array, and every element of that array must itself be an
    object with a ``message`` object carrying a string ``content`` field.
    ``usage`` and ``finish_reason`` are optional (serde ``#[serde(default)]``)
    and are not checked here. Deliberately does NOT check ``choices`` for
    non-emptiness — an empty-but-well-shaped list is the separate ``NoChoices``
    case, checked by the caller once this returns True.
    """
    if not isinstance(data, dict) or "choices" not in data:
        return False
    choices = data["choices"]
    if not isinstance(choices, list):
        return False
    for choice in choices:
        if not isinstance(choice, dict) or "message" not in choice:
            return False
        message = choice["message"]
        if not isinstance(message, dict) or "content" not in message:
            return False
        if not isinstance(message["content"], str):
            return False
    return True


class LLMClient:
    """Async client for an OpenAI-compatible /v1/chat/completions endpoint."""

    def __init__(
        self,
        url: str,
        model: str = "",
        api_key: str = "",
        http_client: httpx.AsyncClient | None = None,
    ) -> None:
        self.url = url.rstrip("/")
        self.model = model
        self.api_key = api_key
        # Accept an externally-managed client (e.g. with connection pooling)
        # or create a private one if none is provided.
        self._external_client = http_client is not None
        self._client = http_client or httpx.AsyncClient(timeout=120.0)

    async def close(self) -> None:
        """Close the underlying HTTP client (no-op if externally managed)."""
        if not self._external_client:
            await self._client.aclose()

    def _auth_headers(self) -> dict[str, str]:
        if self.api_key:
            return {"Authorization": f"Bearer {self.api_key}"}
        return {}

    async def resolve_model(self) -> str:
        """Fetch the first available model from /v1/models and cache it."""
        endpoint = f"{self.url}/v1/models"
        response = await self._client.get(endpoint, headers=self._auth_headers())
        response.raise_for_status()
        data = response.json()
        models = data.get("data", [])
        if not models:
            raise RuntimeError("No models available on the LLM server")
        self.model = models[0]["id"]
        return self.model

    async def translate(self, system: str, user: str, max_tokens: int | None = None) -> str:
        """Run a chat completion and return the assistant's reply text.

        Raises:
            errors.LlmError: on any upstream failure (transport, non-2xx
                status, unparseable body, or an empty ``choices`` list) — the
                taxonomy transcribed from ``llm_api.rs::ApiError``.
        """
        endpoint = f"{self.url}/v1/chat/completions"
        payload: dict[str, object] = {
            "model": self.model,
            "messages": [
                {"role": "system", "content": system},
                {"role": "user", "content": user},
            ],
            "temperature": 0.0,
            "chat_template_kwargs": {"enable_thinking": False},
        }
        if max_tokens is not None:
            payload["max_tokens"] = max_tokens

        try:
            response = await self._client.post(
                endpoint,
                json=payload,
                headers=self._auth_headers(),
            )
        except httpx.InvalidURL as exc:
            # Not an httpx.HTTPError subclass (it's a plain Exception), so it
            # would otherwise escape translate() as a non-LlmError. Rust has no
            # equivalent failure mode pre-request (a malformed --llm-url fails
            # reqwest::Client construction at startup, not per-request), so this
            # is mapped to the same bucket as a connect failure: unreachable.
            raise errors.unreachable(str(exc)) from exc
        except httpx.HTTPError as exc:
            raise errors.map_llm_failure(exc) from exc

        if not response.is_success:
            raise errors.remote_status(response.status_code, response.text)

        def _parse_error() -> errors.LlmError:
            # Mirrors reqwest's ``error decoding response body for url (<url>)``
            # template verbatim, substituting this instance's own upstream URL —
            # the only part of the observed Rust text that is inherently
            # per-instance (see errors.py's module docstring / task-6 report).
            # Confirmed empirically (Fix wave 2) that Rust emits this EXACT
            # generic message for every completion-body deserialize failure —
            # a JSON-syntax error AND every structural mismatch against the
            # strict ChatCompletionResponse shape (missing `choices`, a choice
            # without `message`, a message without `content`, `content: null`,
            # a non-object body) alike. There is no interpolated serde detail
            # (e.g. "missing field `choices`") in the observed text — reqwest's
            # Response::json() decode-error Display doesn't include the source
            # chain. See .superpowers/sdd/task-6-report.md, Fix wave 2.
            return errors.bad_response(f"error decoding response body for url ({response.url})")

        try:
            data = response.json()
        except ValueError as exc:
            raise _parse_error() from exc

        # Deserialize the WHOLE body into the strict completion shape before
        # ever looking at `choices`' contents — matches Rust's
        # `response.json::<ChatCompletionResponse>()`, which fails on any
        # structural mismatch (ApiError::Parse) BEFORE the NoChoices check.
        # `choices` present-but-empty is the only path to `empty_choices()`.
        if not _is_valid_completion_shape(data):
            raise _parse_error()

        choices = data["choices"]
        if not choices:
            raise errors.empty_choices()
        return choices[0]["message"]["content"]
