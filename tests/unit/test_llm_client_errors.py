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


class _RespondingClient:
    """Duck-types httpx.AsyncClient's ``post``; always returns a canned Response."""

    def __init__(self, response: httpx.Response) -> None:
        self._response = response

    async def post(self, *args: object, **kwargs: object) -> httpx.Response:
        return self._response


def _client_returning(body: bytes, status: int = 200) -> LLMClient:
    response = httpx.Response(
        status,
        content=body,
        request=httpx.Request("POST", "http://127.0.0.1:1/v1/chat/completions"),
    )
    return LLMClient(
        url="http://127.0.0.1:1",
        model="m",
        http_client=_RespondingClient(response),  # type: ignore[arg-type]
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


async def test_invalid_url_maps_to_unreachable_503() -> None:
    # MINOR fix-wave-2 finding: httpx.InvalidURL is a plain Exception, NOT an
    # httpx.HTTPError subclass, so the pre-fix code let it escape translate()
    # as a non-LlmError entirely (violating the "LlmError only" contract).
    exc = httpx.InvalidURL("bad url")
    with pytest.raises(LlmError) as exc_info:
        await _client(exc).translate("sys", "usr")
    assert exc_info.value.http_status == 503
    assert exc_info.value.message == f"LLM API unreachable: {exc}"


# ---------------------------------------------------------------------------
# CRITICAL fix-wave-2 finding: strict completion-shape validation. A body with
# no `choices` key (or any other structural mismatch) is a Parse failure, NOT
# the present-and-empty NoChoices case — `data.get("choices", [])` used to
# conflate the two, and blind `choices[0]["message"]["content"]` indexing
# raised a bare KeyError (escaping as a non-LlmError) on every other shape.
# See .superpowers/sdd/task-6-report.md, Fix wave 2, and the differential
# `test_completion_shape_mismatch_parity` for the oracle-verified message text.
# ---------------------------------------------------------------------------


async def test_missing_choices_key_is_bad_response_not_empty_choices() -> None:
    with pytest.raises(LlmError) as exc_info:
        await _client_returning(b"{}").translate("sys", "usr")
    assert exc_info.value.http_status == 500
    assert exc_info.value.message.startswith("LLM API response parse error:")


async def test_choice_without_message_is_bad_response() -> None:
    with pytest.raises(LlmError) as exc_info:
        await _client_returning(b'{"choices":[{}]}').translate("sys", "usr")
    assert exc_info.value.http_status == 500
    assert exc_info.value.message.startswith("LLM API response parse error:")


async def test_message_without_content_is_bad_response() -> None:
    with pytest.raises(LlmError) as exc_info:
        await _client_returning(b'{"choices":[{"message":{}}]}').translate("sys", "usr")
    assert exc_info.value.http_status == 500
    assert exc_info.value.message.startswith("LLM API response parse error:")


async def test_null_content_is_bad_response() -> None:
    body = b'{"choices":[{"message":{"content":null}}]}'
    with pytest.raises(LlmError) as exc_info:
        await _client_returning(body).translate("sys", "usr")
    assert exc_info.value.http_status == 500
    assert exc_info.value.message.startswith("LLM API response parse error:")


async def test_non_object_body_is_bad_response() -> None:
    for body in (b"null", b'"str"', b"[1,2]"):
        with pytest.raises(LlmError) as exc_info:
            await _client_returning(body).translate("sys", "usr")
        assert exc_info.value.http_status == 500
        assert exc_info.value.message.startswith("LLM API response parse error:")


async def test_present_empty_choices_is_still_empty_choices() -> None:
    # Regression guard: the ONE shape that must still map to NoChoices.
    with pytest.raises(LlmError) as exc_info:
        await _client_returning(b'{"choices":[]}').translate("sys", "usr")
    assert exc_info.value.http_status == 500
    assert exc_info.value.message == "LLM API returned no choices"
