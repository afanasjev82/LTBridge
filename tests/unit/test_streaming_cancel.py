"""Unit coverage for the grace-race primitive that the differential harness
cannot reach: client disconnect (no HTTP client can reliably prove the upstream
call was aborted from the outside) and the exact bytes of the slow-failure
fallback chunk.

NOTE on the fixture the brief sketched: a :class:`litestar.response.Stream`
stores its iterator on ``.iterator``, not ``.content`` — ``Stream.__init__``
passes ``content=b""`` up to ``Response.__init__`` and keeps the iterable
separately. ``resp.content`` is therefore ``b""``; the async generator under
test is ``resp.iterator``.

v0.1.5 adds the slow-failure fallback hook (``on_fallback``), the opt-in
``"fallback"`` body marker (``Config.mark_fallback``) and the fallback WARNING
line. Those cases sit at the bottom of this file; each names the mutation it
was observed to fail under. The route-level ones drive ``/translate`` through
the real router as raw ASGI, so they see the exact chunks on the wire.
"""

from __future__ import annotations

import asyncio
import gc
import json
import logging
import re
from typing import Any

import httpx
import pytest
from litestar import Litestar
from litestar.response import Response, Stream
from litestar.serialization import default_serializer, encode_json
from ltbridge.config import Config
from ltbridge.core.errors import (
    FALLBACK_LLM_ERROR,
    FALLBACK_LLM_TIMEOUT,
    LlmError,
    fallback_reason,
    remote_status,
    timeout,
    unreachable,
)
from ltbridge.llm_client import LLMClient
from ltbridge.server import create_router, exception_handlers
from ltbridge.server.streaming import GRACE_PERIOD_S, HEARTBEAT_INTERVAL_S, respond_with_grace


async def test_disconnect_cancels_work() -> None:
    started = asyncio.Event()
    cancelled = asyncio.Event()

    async def work() -> dict:
        started.set()
        try:
            await asyncio.sleep(60)
        except asyncio.CancelledError:
            cancelled.set()
            raise
        return {"translatedText": "never"}

    resp = await respond_with_grace(work(), fallback_body={"translatedText": "orig"})
    assert isinstance(resp, Stream)
    gen = resp.iterator  # the async generator behind the Stream
    assert started.is_set()
    assert await gen.__anext__() == b" "  # first heartbeat
    await gen.aclose()  # simulates client disconnect
    await asyncio.wait_for(cancelled.wait(), timeout=2)


async def test_cancel_during_grace_period_cancels_work() -> None:
    """A disconnect BEFORE anything is committed must also abort the upstream
    call — Rust's ``select!`` drops the future when the handler is dropped.
    """
    cancelled = asyncio.Event()

    async def work() -> dict:
        try:
            await asyncio.sleep(60)
        except asyncio.CancelledError:
            cancelled.set()
            raise
        return {"translatedText": "never"}

    task = asyncio.create_task(respond_with_grace(work(), fallback_body={}))
    await asyncio.sleep(0.1)  # inside the 1s grace window
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    await asyncio.wait_for(cancelled.wait(), timeout=2)


async def test_slow_failure_streams_fallback_body_verbatim() -> None:
    """The wart, at byte level: a failure that arrives after the 200 is
    committed yields the fallback body serialized exactly as the buffered path
    would serialize it (compact, raw UTF-8 — not ``json.dumps`` defaults).
    """
    fallback = {"detectedLanguage": {"confidence": 3, "language": "da"}, "translatedText": "Привет"}

    async def work() -> dict:
        await asyncio.sleep(1.2)
        raise RuntimeError("upstream died slowly")

    resp = await respond_with_grace(work(), fallback_body=fallback)
    assert isinstance(resp, Stream)
    chunks = [chunk async for chunk in resp.iterator]
    assert chunks[:-1] == [b" "] * (len(chunks) - 1)
    assert chunks[-1] == encode_json(fallback, default_serializer)
    assert b"\\u" not in chunks[-1]


async def test_fast_path_is_buffered_200_not_201() -> None:
    """Litestar defaults a POST handler's response to 201; the grace-race must
    pin 200 the way Rust's ``HttpResponse::Ok()`` does.
    """
    body = {"translatedText": "Hi"}

    async def work() -> dict:
        return body

    resp = await respond_with_grace(work(), fallback_body={"translatedText": "orig"})
    assert isinstance(resp, Response) and not isinstance(resp, Stream)
    assert resp.status_code == 200
    assert resp.content == body


async def test_disconnect_after_work_already_failed_no_unretrieved_exception_warning() -> None:
    """Guards the ``task.add_done_callback`` fix: if the work task fails while
    the heartbeat generator is merely PARKED between ``yield``s (not actively
    inside the ``asyncio.wait`` that would otherwise consume it via
    ``task.result()``), and the client then disconnects, asyncio must not log
    "Task exception was never retrieved" once the task is garbage-collected.

    Without the done-callback, this is exactly the gap: the generator's
    ``finally`` only calls ``task.cancel()`` when ``not task.done()`` — a task
    that already finished (with an exception) slips past both that guard AND
    the `try: task.result() except Exception` branch, since a disconnect
    throws ``GeneratorExit`` in at the ``yield`` point, well before the loop
    ever reaches ``task.result()`` again.
    """
    logged: list[dict[str, object]] = []
    loop = asyncio.get_running_loop()
    original_handler = loop.get_exception_handler()
    loop.set_exception_handler(lambda _loop, context: logged.append(context))

    try:

        async def work() -> dict:
            # Past the grace period AND past the first heartbeat's own window,
            # so the first heartbeat is yielded (and the generator re-parked)
            # before this fails.
            await asyncio.sleep(GRACE_PERIOD_S + HEARTBEAT_INTERVAL_S + 0.3)
            raise RuntimeError("boom after heartbeat")

        resp = await respond_with_grace(work(), fallback_body={"translatedText": "orig"})
        assert isinstance(resp, Stream)
        gen = resp.iterator
        assert await gen.__anext__() == b" "  # first heartbeat; generator now parked at yield

        # Let the work task fail while we are parked between __anext__() calls
        # (not inside an active asyncio.wait that would otherwise retrieve it).
        await asyncio.sleep(0.5)

        await gen.aclose()  # simulate client disconnect while parked

        del gen
        del resp
        gc.collect()  # Task involvement in a traceback can form a reference
        # cycle, so refcounting alone may not finalize it — force the cycle
        # collector so any pending Task.__del__ runs now, not at some later
        # arbitrary GC pass.
        await asyncio.sleep(0)  # let any call_exception_handler dispatch settle

    finally:
        loop.set_exception_handler(original_handler)

    messages = [str(c.get("message", "")) for c in logged]
    assert not any("Task exception was never retrieved" in m for m in messages), (
        f"unretrieved-exception warning logged: {logged}"
    )


async def test_fast_failure_propagates() -> None:
    """A failure inside the grace window must stay an exception so the app's
    ``LlmError`` handler can render the real status — never the wart.
    """

    async def work() -> dict:
        raise RuntimeError("upstream died fast")

    with pytest.raises(RuntimeError, match="died fast"):
        await respond_with_grace(work(), fallback_body={"translatedText": "orig"})


# ---------------------------------------------------------------------------
# v0.1.5: the fallback hook, the opt-in marker, and the fallback log line
# ---------------------------------------------------------------------------

#: Past the 1 s grace (the 200 is committed) but inside the first heartbeat's
#: own window (2 s), so the stream carries exactly one chunk: the final body.
_SLOW_FAIL_S = 1.5
_FALLBACK_LOGGER = "ltbridge.server.streaming"
_PAYLOAD: dict[str, object] = {"q": "orig", "source": "en", "target": "ru"}


class _SlowFailingUpstream:
    """Duck-types ``httpx.AsyncClient.post`` for ``LLMClient``: waits past the
    grace period, then raises ``exc`` or answers ``status``."""

    def __init__(self, *, exc: Exception | None = None, status: int = 502) -> None:
        self._exc = exc
        self._status = status

    async def post(self, url: str, **kwargs: object) -> httpx.Response:
        await asyncio.sleep(_SLOW_FAIL_S)
        if self._exc is not None:
            raise self._exc
        return httpx.Response(
            self._status, content=b"bad gateway", request=httpx.Request("POST", url)
        )


def _config(**overrides: Any) -> Config:
    return Config(llm_url="http://127.0.0.1:1", llm_model="m", **overrides)


async def _post_translate(
    config: Config, upstream: _SlowFailingUpstream, payload: dict[str, object]
) -> tuple[int, bool, list[bytes]]:
    """POST ``/translate`` through ``create_router`` as raw ASGI.

    Returns ``(status, streamed, chunks)``: ``streamed`` is True when any body
    message announced ``more_body`` (the chunked path), ``chunks`` the
    non-empty body payloads in wire order.
    """
    llm = LLMClient(url=config.llm_url, model="m", http_client=upstream)  # type: ignore[arg-type]
    app = Litestar(
        route_handlers=[create_router(config, llm)],
        exception_handlers=exception_handlers(),
        logging_config=None,
    )
    raw = json.dumps(payload).encode()
    scope = {
        "type": "http",
        "asgi": {"version": "3.0"},
        "http_version": "1.1",
        "method": "POST",
        "scheme": "http",
        "path": "/translate",
        "raw_path": b"/translate",
        "root_path": "",
        "query_string": b"",
        "headers": [
            (b"content-type", b"application/json"),
            (b"content-length", str(len(raw)).encode()),
        ],
        "client": ("127.0.0.1", 50000),
        "server": ("127.0.0.1", 5050),
    }
    complete = asyncio.Event()
    delivered = False
    messages: list[dict[str, Any]] = []

    async def receive() -> dict[str, Any]:
        nonlocal delivered
        if not delivered:
            delivered = True
            return {"type": "http.request", "body": raw, "more_body": False}
        # Litestar's Stream listens for a disconnect until the response is out;
        # a real server reports one only after the last body message.
        await complete.wait()
        return {"type": "http.disconnect"}

    async def send(message: dict[str, Any]) -> None:
        messages.append(message)
        if message["type"] == "http.response.body" and not message.get("more_body", False):
            complete.set()

    await app(scope, receive, send)  # type: ignore[arg-type]
    status = next(m["status"] for m in messages if m["type"] == "http.response.start")
    bodies = [m for m in messages if m["type"] == "http.response.body"]
    streamed = any(m.get("more_body", False) for m in bodies)
    return status, streamed, [m["body"] for m in bodies if m.get("body")]


def _with_cause(exc: Exception, cause: BaseException) -> Exception:
    exc.__cause__ = cause
    return exc


@pytest.mark.parametrize(
    "exc,expected",
    [
        (httpx.ReadTimeout("t"), FALLBACK_LLM_TIMEOUT),
        (_with_cause(timeout(), httpx.ReadTimeout("t")), FALLBACK_LLM_TIMEOUT),
        (_with_cause(timeout(), httpx.ConnectTimeout("t")), FALLBACK_LLM_TIMEOUT),
        (_with_cause(timeout(), httpx.PoolTimeout("t")), FALLBACK_LLM_TIMEOUT),
        (remote_status(502, "bad gateway"), FALLBACK_LLM_ERROR),
        (_with_cause(unreachable("x"), httpx.ConnectError("x")), FALLBACK_LLM_ERROR),
        (LlmError(500, "LLM API returned no choices"), FALLBACK_LLM_ERROR),
        (RuntimeError("x"), FALLBACK_LLM_ERROR),
    ],
    ids=[
        "bare_read_timeout",
        "llm_timeout_from_read_timeout",
        "llm_timeout_from_connect_timeout",
        "llm_timeout_from_pool_timeout",
        "remote_502",
        "unreachable_from_connect_error",
        "no_choices",
        "unexpected",
    ],
)
def test_fallback_reason_classifies_exc_and_its_cause(exc: Exception, expected: str) -> None:
    """``LLMClient`` raises ``errors.timeout() from exc``, so the httpx timeout
    sits on ``__cause__``; a bare one counts too; everything else is an error.
    """
    assert fallback_reason(exc) == expected


async def test_slow_timeout_flag_on_marks_llm_timeout() -> None:
    """A ``ReadTimeout`` after 1.5 s, flag on: the one chunk is the marked echo.

    MUTATION: ``fallback_reason`` always returns ``llm_error`` -> observed red
    (8 failed: this test, the auto-source one, both log params, and the four
    timeout ids of ``test_fallback_reason_classifies_exc_and_its_cause``).
    """
    upstream = _SlowFailingUpstream(exc=httpx.ReadTimeout("t"))
    status, streamed, chunks = await _post_translate(
        _config(mark_fallback=True), upstream, _PAYLOAD
    )
    assert status == 200 and streamed
    assert chunks[-1] == b'{"fallback":"llm_timeout","translatedText":"orig"}'


async def test_slow_remote_502_flag_on_marks_llm_error() -> None:
    """``remote_status(502)`` after 1.5 s carries no timeout cause -> ``llm_error``."""
    status, streamed, chunks = await _post_translate(
        _config(mark_fallback=True), _SlowFailingUpstream(status=502), _PAYLOAD
    )
    assert status == 200 and streamed
    assert chunks[-1] == b'{"fallback":"llm_error","translatedText":"orig"}'


async def test_slow_timeout_flag_on_auto_source_keeps_key_order() -> None:
    """``source="auto"`` + ``alternatives``: the marker lands between
    ``detectedLanguage`` and ``translatedText``, and the text is ``q`` as the
    formatting pass returns it (padding stripped)."""
    payload: dict[str, object] = {
        "q": "  orig  ", "source": "auto", "target": "ru", "alternatives": 2,
    }
    upstream = _SlowFailingUpstream(exc=httpx.ReadTimeout("t"))
    _status, streamed, chunks = await _post_translate(
        _config(mark_fallback=True), upstream, payload
    )
    assert streamed
    body = json.loads(chunks[-1])
    assert list(body) == ["alternatives", "detectedLanguage", "fallback", "translatedText"]
    assert body["fallback"] == "llm_timeout"
    assert body["translatedText"] == "orig"


async def test_slow_timeout_flag_off_chunk_is_byte_identical_to_v014() -> None:
    """Flag off (the default): the chunk is the v0.1.4 / Rust fallback, no marker.

    MUTATION: mark the body regardless of the flag -> observed red (this test
    only; the chunk gains ``"fallback":"llm_timeout"``).
    """
    upstream = _SlowFailingUpstream(exc=httpx.ReadTimeout("t"))
    status, streamed, chunks = await _post_translate(_config(), upstream, _PAYLOAD)
    assert status == 200 and streamed
    assert chunks == [b'{"translatedText":"orig"}']


@pytest.mark.parametrize("mark", [True, False], ids=["flag_on", "flag_off"])
async def test_slow_failure_logs_exactly_one_warning_without_request_text(
    mark: bool, caplog: pytest.LogCaptureFixture
) -> None:
    """Every fallback logs ONE WARNING on ``ltbridge.server.streaming``, flag on
    or off, carrying the reason, the cause's class, the timing, the length and
    the language codes, and neither the request text nor the exception message.

    MUTATION: drop the log call -> observed red (both params: 0 records).
    """
    q = "zebra quartz sentinel text"
    exc_detail = "exc-detail-sentinel"
    caplog.set_level(logging.DEBUG)
    upstream = _SlowFailingUpstream(exc=httpx.ReadTimeout(exc_detail))
    await _post_translate(
        _config(mark_fallback=mark), upstream, {"q": q, "source": "en", "target": "ru"}
    )

    records = [
        r for r in caplog.records if r.name == _FALLBACK_LOGGER and r.levelno == logging.WARNING
    ]
    assert len(records) == 1
    # Counted, never printed: a failing assert must not echo the text it guards.
    leaks = sum(secret in caplog.text for secret in (q, exc_detail))
    assert leaks == 0, "request text or exception message reached the log"
    assert records[0].exc_info is None
    line = records[0].getMessage()
    assert re.fullmatch(
        r"translate fallback served \(reason=llm_timeout error=ReadTimeout "
        rf"elapsed_s=\d+\.\d chars={len(q)} src=en tgt=ru\)",
        line,
    ), line


async def test_fallback_hook_body_is_served_with_exception_and_elapsed() -> None:
    """The hook gets the work's exception and the seconds since ``work``
    started, and its body is what goes on the wire."""
    seen: list[tuple[type[BaseException], float]] = []

    def hook(exc: Exception, elapsed_s: float) -> dict[str, Any]:
        seen.append((type(exc), elapsed_s))
        return {"fallback": "llm_error", "translatedText": "orig"}

    async def work() -> dict:
        await asyncio.sleep(1.2)
        raise RuntimeError("upstream died slowly")

    resp = await respond_with_grace(
        work(), fallback_body={"translatedText": "orig"}, on_fallback=hook
    )
    assert isinstance(resp, Stream)
    chunks = [chunk async for chunk in resp.iterator]
    assert chunks[-1] == b'{"fallback":"llm_error","translatedText":"orig"}'
    assert len(seen) == 1 and seen[0][0] is RuntimeError
    assert 1.1 <= seen[0][1] < 3.0


async def test_raising_fallback_hook_still_serves_fallback_body() -> None:
    """The 200 is committed: a hook that raises degrades to ``fallback_body``.

    MUTATION: drop the guard around the hook -> observed red (this test only:
    the hook's RuntimeError escapes the stream instead of a final chunk).
    """
    fallback = {"translatedText": "orig"}

    def hook(exc: Exception, elapsed_s: float) -> dict[str, Any]:
        raise RuntimeError("hook bug")

    async def work() -> dict:
        await asyncio.sleep(1.2)
        raise RuntimeError("upstream died slowly")

    resp = await respond_with_grace(work(), fallback_body=fallback, on_fallback=hook)
    assert isinstance(resp, Stream)
    chunks = [chunk async for chunk in resp.iterator]
    assert chunks[-1] == encode_json(fallback, default_serializer)


async def test_fast_failure_never_calls_the_fallback_hook() -> None:
    """Inside the grace window the exception still propagates (a real 5xx)."""
    calls: list[Exception] = []

    def hook(exc: Exception, elapsed_s: float) -> dict[str, Any]:
        calls.append(exc)
        return {}

    async def work() -> dict:
        raise RuntimeError("upstream died fast")

    with pytest.raises(RuntimeError, match="died fast"):
        await respond_with_grace(work(), fallback_body={}, on_fallback=hook)
    assert calls == []
