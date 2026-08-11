"""Unit coverage for llm_client.translate()'s transport-error dispatch.

Covers the connect-refused and read-timeout cases the Task 6 brief explicitly
defers from differential coverage:

- connect-refused: cannot be tested differentially — pointing a Rust server at
  a dead LLM URL makes it exit at startup during model resolution (confirmed
  via the dump-capture throwaway script), so there is no live Rust process to
  compare against.
- read-timeout: at >1s it lands in the Rust grace-race's chunked-200 "wart"
  path (Task 8), not this task's fast-failure taxonomy (confirmed via the
  dump-capture throwaway script: --llm-timeout 2 + a 5s mock delay yields a
  200 with the original untranslated text, not a 503).

Both are exercised here via a stub HTTP client that raises the target httpx
exception directly, monkeypatch-style, instead of a real socket.
"""

from __future__ import annotations

import httpx
import pytest
from ltengine.errors import LlmError
from ltengine.llm_client import LLMClient


class _RaisingClient:
    """Duck-types httpx.AsyncClient's ``post`` just enough for LLMClient; always raises."""

    def __init__(self, exc: Exception) -> None:
        self._exc = exc

    async def post(self, *args: object, **kwargs: object) -> httpx.Response:
        raise self._exc


def _client(exc: Exception) -> LLMClient:
    return LLMClient(
        url="http://127.0.0.1:1",
        model="m",
        http_client=_RaisingClient(exc),  # type: ignore[arg-type]
    )


async def test_connect_refused_maps_to_unreachable_503() -> None:
    exc = httpx.ConnectError("Connection refused")
    with pytest.raises(LlmError) as exc_info:
        await _client(exc).translate("sys", "usr")
    assert exc_info.value.http_status == 503
    assert exc_info.value.message == f"LLM API unreachable: {exc}"


async def test_read_timeout_maps_to_timeout_503() -> None:
    exc = httpx.ReadTimeout("timed out")
    with pytest.raises(LlmError) as exc_info:
        await _client(exc).translate("sys", "usr")
    assert exc_info.value.http_status == 503
    assert exc_info.value.message == "LLM API request timed out"


async def test_connect_timeout_is_a_timeout_not_unreachable() -> None:
    # reqwest's is_timeout() is true for connect timeouts too, so Rust maps
    # this to ApiError::Timeout, not Unreachable — see
    # errors.map_llm_failure's docstring. httpx.ConnectTimeout is a
    # TimeoutException subclass, so it must take the timeout() branch.
    exc = httpx.ConnectTimeout("connect timed out")
    with pytest.raises(LlmError) as exc_info:
        await _client(exc).translate("sys", "usr")
    assert exc_info.value.http_status == 503
    assert exc_info.value.message == "LLM API request timed out"
