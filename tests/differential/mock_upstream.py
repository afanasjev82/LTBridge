"""Scriptable, recording mock of an OpenAI-compatible upstream (raw ASGI)."""
from __future__ import annotations

import asyncio
import json
from dataclasses import dataclass, field
from typing import Any


@dataclass
class MockBehavior:
    reply_text: str = "MOCK_TRANSLATION"
    status: int = 200
    delay_s: float = 0.0
    body_mode: str = "ok"  # ok | garbage | empty_choices | custom
    custom_body: str = ""


@dataclass
class MockState:
    behavior: MockBehavior = field(default_factory=MockBehavior)
    requests: list[dict[str, Any]] = field(default_factory=list)


def _ok_body(text: str) -> bytes:
    return json.dumps({
        "id": "cmpl-mock", "object": "chat.completion", "model": "mock-model",
        "choices": [{"index": 0, "finish_reason": "stop",
                     "message": {"role": "assistant", "content": text}}],
    }).encode()


MODELS_BODY = json.dumps({
    "object": "list",
    "data": [{"id": "mock-model", "object": "model"}],
}).encode()


def create_mock_app(state: MockState):
    async def send_json(send, status: int, body: bytes) -> None:
        await send({"type": "http.response.start", "status": status,
                    "headers": [(b"content-type", b"application/json")]})
        await send({"type": "http.response.body", "body": body})

    async def read_body(receive) -> bytes:
        chunks: list[bytes] = []
        while True:
            msg = await receive()
            if msg["type"] == "http.request":
                chunks.append(msg.get("body", b""))
                if not msg.get("more_body"):
                    break
            elif msg["type"] == "http.disconnect":
                break
        return b"".join(chunks)

    async def app(scope, receive, send) -> None:
        if scope["type"] != "http":
            return
        path, method = scope["path"], scope["method"]
        if path == "/v1/models" and method == "GET":
            await send_json(send, 200, MODELS_BODY)
        elif path == "/v1/chat/completions" and method == "POST":
            raw = await read_body(receive)
            state.requests.append(json.loads(raw))
            b = state.behavior
            if b.delay_s:
                await asyncio.sleep(b.delay_s)
            if b.body_mode == "garbage":
                await send_json(send, b.status, b"this is not json {{{")
            elif b.body_mode == "empty_choices":
                await send_json(send, b.status, json.dumps({"choices": []}).encode())
            elif b.body_mode == "custom":
                await send_json(send, b.status, b.custom_body.encode())
            else:
                await send_json(send, b.status, _ok_body(b.reply_text))
        elif path == "/__mock__/behavior" and method == "POST":
            state.behavior = MockBehavior(**json.loads(await read_body(receive)))
            await send_json(send, 200, b"{}")
        elif path == "/__mock__/reset" and method == "POST":
            state.behavior = MockBehavior()
            state.requests.clear()
            await send_json(send, 200, b"{}")
        elif path == "/__mock__/requests" and method == "GET":
            await send_json(send, 200, json.dumps(state.requests).encode())
        else:
            await send_json(send, 404, b'{"error":"mock: unknown path"}')

    return app
