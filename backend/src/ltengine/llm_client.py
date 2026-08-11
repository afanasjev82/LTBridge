"""Async OpenAI-compatible LLM API client.

Port of LTEngine's llm_api.rs — calls an external OpenAI-compatible server
(e.g. vLLM, llama-server, Ollama, OpenAI) to perform translation.
"""

from __future__ import annotations

import httpx


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
        """Run a chat completion and return the assistant's reply text."""
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
        response = await self._client.post(
            endpoint,
            json=payload,
            headers=self._auth_headers(),
        )
        response.raise_for_status()
        data = response.json()
        choices = data.get("choices", [])
        if not choices:
            raise RuntimeError("LLM API returned no choices")
        return choices[0]["message"]["content"]
