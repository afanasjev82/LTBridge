from __future__ import annotations

import json
import re
from dataclasses import dataclass
from typing import Any

import httpx

from .conftest import ServerPair

#: Matches the "http://host:port" prefix of an upstream URL embedded in an
#: error message (e.g. reqwest's "error decoding response body for url
#: (http://127.0.0.1:54321/v1/chat/completions)"). Used to normalize away the
#: one genuinely non-reproducible substring across the two independent mock
#: instances the differential harness spins up (see test_error_parity.py's
#: garbage_json case).
_UPSTREAM_URL_RE = re.compile(rb"http://127\.0\.0\.1:\d+")


@dataclass
class Snapshot:
    status: int
    content_type: str
    body: bytes
    chunked: bool
    leading_spaces: int
    json: Any | None


def _snap(resp: httpx.Response) -> Snapshot:
    body = resp.content
    stripped = body.lstrip(b" ")
    leading = len(body) - len(stripped)
    try:
        parsed = json.loads(stripped) if stripped else None
    except json.JSONDecodeError:
        parsed = None
    return Snapshot(
        status=resp.status_code,
        content_type=resp.headers.get("content-type", "").split(";")[0].strip(),
        body=body,
        chunked=resp.headers.get("transfer-encoding", "") == "chunked",
        leading_spaces=leading,
        json=parsed,
    )


def _fire(base_url: str, method: str, path: str, *, json_body=None, form=None,
          files=None, headers=None, content: bytes | None = None,
          timeout: float = 60.0) -> Snapshot:
    with httpx.Client(timeout=timeout) as c:
        resp = c.request(method, f"{base_url}{path}", json=json_body, data=form,
                         files=files, headers=headers, content=content)
    return _snap(resp)


def fire_both(pair: ServerPair, method: str, path: str, **kw) -> tuple[Snapshot, Snapshot]:
    return (_fire(pair.rust.base_url, method, path, **kw),
            _fire(pair.py.base_url, method, path, **kw))


def assert_parity(rust: Snapshot, py: Snapshot, *, normalize_heartbeat: bool = False,
                  normalize_upstream_url: bool = False) -> None:
    assert py.status == rust.status, f"status: py={py.status} rust={rust.status}"
    assert py.content_type == rust.content_type, (
        f"content-type: py={py.content_type!r} rust={rust.content_type!r}")
    if normalize_heartbeat:
        # Heartbeat space count is timing-jittery; require both chunked with >=1 space,
        # then compare the JSON payloads exactly.
        assert rust.chunked and py.chunked, "expected chunked responses on both"
        assert rust.leading_spaces >= 1 and py.leading_spaces >= 1
        assert py.json == rust.json, f"json: py={py.json} rust={rust.json}"
    elif normalize_upstream_url:
        # rust and py each hit their OWN independent mock (different random
        # ports — see conftest.py's `_make`), so a message that embeds the
        # upstream URL can never be byte-identical on that substring alone.
        # Strip it from both sides before comparing the rest of the body.
        rust_body = _UPSTREAM_URL_RE.sub(b"http://<MOCK>", rust.body)
        py_body = _UPSTREAM_URL_RE.sub(b"http://<MOCK>", py.body)
        assert py_body == rust_body, f"body (url-normalized): py={py_body!r} rust={rust_body!r}"
    else:
        assert py.body == rust.body, f"body: py={py.body!r} rust={rust.body!r}"


def last_payload(handle) -> dict:
    reqs = handle.mock.requests()
    assert reqs, "no chat-completions request recorded"
    return reqs[-1]
