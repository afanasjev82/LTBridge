"""Unit coverage for reasoning-model handling in llm_client.translate().

Two independent layers, because backends disagree on how thinking is switched
off and where its text ends up:

- ``reasoning_effort`` (opt-in, ``--llm-reasoning-effort``): the OpenAI Chat
  Completions field. LM Studio IGNORES the vLLM/SGLang
  ``chat_template_kwargs.enable_thinking`` extension LTBridge always sends
  (verified live against LM Studio + ``qwen/qwen3.8-27b``: 160 reasoning tokens
  for "Hello, world!") but honours ``reasoning_effort: "none"`` (0 reasoning
  tokens). Omitted by default so the request stays byte-identical to the Rust
  reference (tests/differential/test_payload_parity.py) and so servers that
  reject the field (older vLLM, non-reasoning OpenAI models) keep working.
- A leading ``<think>...</think>`` block in ``message.content`` is stripped.
  Backends that separate reasoning (LM Studio, vLLM with ``--reasoning-parser``)
  put it in ``reasoning_content``/``reasoning`` and never reach this; the ones
  that don't would otherwise hand the model's chain of thought back to the
  client as the "translation".
"""

from __future__ import annotations

import httpx
import pytest
from ltbridge.config import Config
from ltbridge.core.errors import LlmError
from ltbridge.llm_client import LLMClient
from ltbridge.server import build_llm_client


class _CapturingClient:
    """Duck-types httpx.AsyncClient's ``post``; records the payload, returns ``content``."""

    def __init__(self, content: str = "ok") -> None:
        self.payloads: list[dict[str, object]] = []
        self._content = content

    async def post(self, url: str, *, json: dict[str, object], **kwargs: object) -> httpx.Response:
        self.payloads.append(json)
        body = {"choices": [{"message": {"content": self._content}, "finish_reason": "stop"}]}
        return httpx.Response(200, json=body, request=httpx.Request("POST", url))


def _client(content: str = "ok", reasoning_effort: str = "") -> tuple[LLMClient, _CapturingClient]:
    http = _CapturingClient(content)
    llm = LLMClient(
        url="http://127.0.0.1:1",
        model="m",
        reasoning_effort=reasoning_effort,
        http_client=http,  # type: ignore[arg-type]
    )
    return llm, http


# ---------------------------------------------------------------------------
# reasoning_effort payload field
# ---------------------------------------------------------------------------


async def test_reasoning_effort_omitted_by_default() -> None:
    llm, http = _client()
    await llm.translate("sys", "usr")
    assert "reasoning_effort" not in http.payloads[0]
    # The vLLM/SGLang switch is still sent unconditionally (Rust parity).
    assert http.payloads[0]["chat_template_kwargs"] == {"enable_thinking": False}


async def test_reasoning_effort_sent_verbatim_when_configured() -> None:
    llm, http = _client(reasoning_effort="none")
    await llm.translate("sys", "usr")
    assert http.payloads[0]["reasoning_effort"] == "none"
    assert http.payloads[0]["chat_template_kwargs"] == {"enable_thinking": False}


async def test_build_llm_client_forwards_reasoning_effort_from_config() -> None:
    http = _CapturingClient()
    cfg = Config(llm_url="http://127.0.0.1:1", llm_model="m", llm_reasoning_effort="low")
    llm = build_llm_client(cfg, http)  # type: ignore[arg-type]
    await llm.translate("sys", "usr")
    assert http.payloads[0]["reasoning_effort"] == "low"


def test_config_from_env_reads_reasoning_effort(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("LTBRIDGE_LLM_URL", "http://127.0.0.1:1")
    monkeypatch.setenv("LTBRIDGE_LLM_REASONING_EFFORT", "none")
    assert Config.from_env().llm_reasoning_effort == "none"


def test_config_from_env_reasoning_effort_defaults_empty(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("LTBRIDGE_LLM_URL", "http://127.0.0.1:1")
    monkeypatch.delenv("LTBRIDGE_LLM_REASONING_EFFORT", raising=False)
    assert Config.from_env().llm_reasoning_effort == ""


# ---------------------------------------------------------------------------
# <think> block stripping
# ---------------------------------------------------------------------------


async def test_leading_think_block_is_stripped() -> None:
    llm, _ = _client("<think>\nWe need to translate.\nOK.\n</think>\n\nПривет, мир!")
    assert await llm.translate("sys", "usr") == "Привет, мир!"


async def test_leading_whitespace_before_think_block_is_tolerated() -> None:
    llm, _ = _client("\n  <think>reasoning</think>Привет")
    assert await llm.translate("sys", "usr") == "Привет"


async def test_orphan_closing_tag_strips_everything_before_it() -> None:
    # Templates that open `<think>` inside the generation prompt (DeepSeek-R1,
    # QwQ, some Qwen3 builds) leave only the closing tag in the completion.
    llm, _ = _client("We need to translate this.\n</think>\n\nПривет")
    assert await llm.translate("sys", "usr") == "Привет"


async def test_unterminated_think_block_is_a_bad_response() -> None:
    # Truncated mid-reasoning (e.g. max_tokens hit): the whole body is chain of
    # thought, so there is no translation to return. Never pass it through.
    llm, _ = _client("<think>We need to translate this. Let me think about")
    with pytest.raises(LlmError) as exc_info:
        await llm.translate("sys", "usr")
    assert exc_info.value.http_status == 500
    assert exc_info.value.message.startswith("LLM API response parse error:")


@pytest.mark.parametrize(
    "content",
    [
        "Привет, мир!",
        "\n\nПривет, мир!",  # LM Studio's separated-reasoning content keeps its leading newlines
        "Use the <think> tag like this",  # a literal tag mid-text is content, not reasoning
        "",
    ],
)
async def test_content_without_reasoning_is_returned_unchanged(content: str) -> None:
    llm, _ = _client(content)
    assert await llm.translate("sys", "usr") == content
