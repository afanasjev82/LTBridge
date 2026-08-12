"""Unit coverage for the grace-race primitive that the differential harness
cannot reach: client disconnect (no HTTP client can reliably prove the upstream
call was aborted from the outside) and the exact bytes of the slow-failure
fallback chunk.

NOTE on the fixture the brief sketched: a :class:`litestar.response.Stream`
stores its iterator on ``.iterator``, not ``.content`` — ``Stream.__init__``
passes ``content=b""`` up to ``Response.__init__`` and keeps the iterable
separately. ``resp.content`` is therefore ``b""``; the async generator under
test is ``resp.iterator``.
"""

from __future__ import annotations

import asyncio
import gc

import pytest
from litestar.response import Response, Stream
from litestar.serialization import default_serializer, encode_json
from ltengine.streaming import GRACE_PERIOD_S, HEARTBEAT_INTERVAL_S, respond_with_grace


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
