"""Grace-race + heartbeat streaming — port of main.rs's ``GRACE_PERIOD`` logic.

LTEngine's ``/translate`` handler does not simply await the LLM. It races the
upstream call against a one-second timer (``tokio::select!``):

* **resolved within the grace period** (the common case — an upstream error
  lands here almost immediately) → a normal BUFFERED response, so the real HTTP
  status survives (Task 6's error taxonomy, and any downstream failover that
  depends on it) and the body carries no leading whitespace;
* **still pending after the grace period** → Rust commits to a chunked ``200``
  and emits one ``b" "`` per ``HEARTBEAT_INTERVAL`` until the call resolves,
  then writes the JSON body as the final chunk. The heartbeats keep
  intermediaries (and the bundled web frontend) from timing the request out.

The second branch carries a deliberate **wart we must reproduce**: the ``200``
status line is already on the wire by the time a slow call fails, so Rust
cannot report the failure at all — ``res.unwrap_or_else(|_| q.clone())`` falls
back to the ORIGINAL text and the client receives a perfectly ordinary-looking
translation that was never translated. Fast failures keep their clean 5xx;
only >1 s failures degrade this way. See main.rs:364-411.

Dropping the stream (client disconnect) drops the future in Rust, which closes
the reqwest connection so vLLM can abort and free its KV slot. The Python
equivalent is cancelling the :class:`asyncio.Task`, which is what the
generator's ``finally`` does.

v0.1.5 (Python-only): :func:`respond_with_grace` takes an ``on_fallback`` hook,
which the ``/translate`` route uses to log every slow-failure fallback and,
behind the opt-in ``--mark-fallback``, to tag the body with a ``"fallback"``
reason. Without the hook, or with the flag off, the bytes are Rust's.
"""

from __future__ import annotations

import asyncio
import logging
import time
from collections.abc import AsyncGenerator, Callable, Coroutine
from typing import Any

from litestar.response import Response, Stream
from litestar.serialization import default_serializer, encode_json
from litestar.status_codes import HTTP_200_OK

#: ``ltbridge.server.streaming`` — the route's fallback WARNING is logged here
#: too, so one logger name covers every slow-failure event.
logger = logging.getLogger(__name__)

#: main.rs: ``const GRACE_PERIOD: Duration = Duration::from_secs(1)``.
GRACE_PERIOD_S = 1.0
#: main.rs: ``const HEARTBEAT_INTERVAL: Duration = Duration::from_secs(1)``.
HEARTBEAT_INTERVAL_S = 1.0

_JSON_MEDIA_TYPE = "application/json"
_HEARTBEAT = b" "

#: A fully-assembled ``/translate`` response body (Rust's ``translate_response_json``).
ResultBody = dict[str, Any]

#: ``on_fallback(exc, elapsed_s) -> body``: builds the body served when a slow
#: ``work`` fails; ``elapsed_s`` is measured from when ``work`` was started.
FallbackHook = Callable[[Exception, float], ResultBody]


def _encode(body: ResultBody) -> bytes:
    """Serialize a response body exactly as the BUFFERED path would.

    This must be byte-identical to what Litestar emits for the same dict, which
    is ``encode_json(content, default_serializer)`` — msgspec: compact
    separators, raw UTF-8, keys in insertion order (the caller inserts them in
    the alphabetical order serde_json's ``BTreeMap`` produces; see routes.py).
    ``json.dumps`` is NOT interchangeable here: its defaults
    (``ensure_ascii=True`` plus ``", "``/``": "`` separators) diverge from
    serde_json on any non-ASCII translation and on every body with >1 key.
    """
    return encode_json(body, default_serializer)


def _fallback_chunk(
    exc: Exception,
    fallback_body: ResultBody,
    on_fallback: FallbackHook | None,
    elapsed_s: float,
) -> bytes:
    """The final chunk for a slow failure: the hook's body, else ``fallback_body``.

    The ``200`` is already on the wire, so a hook that raises (or returns
    something unserializable) cannot be reported to the client either; it
    degrades to the plain ``fallback_body`` and is logged by exception CLASS
    only (a message can carry the request text).
    """
    if on_fallback is not None:
        try:
            return _encode(on_fallback(exc, elapsed_s))
        except Exception as hook_exc:  # noqa: BLE001 — the stream is committed; serve something
            logger.error(
                "fallback hook raised (error=%s); serving the unmarked fallback body",
                type(hook_exc).__name__,
            )
    return _encode(fallback_body)


async def _heartbeat_stream(
    task: asyncio.Task[ResultBody],
    fallback_body: ResultBody,
    on_fallback: FallbackHook | None,
    started: float,
) -> AsyncGenerator[bytes, None]:
    """Yield one space per heartbeat interval, then the final JSON body.

    Rust arms a ``tokio::time::interval`` here and consumes its immediate first
    tick, so the first space lands one full interval after the grace period —
    the ``await`` -then- ``yield`` order below reproduces that schedule.
    """
    try:
        while True:
            done, _pending = await asyncio.wait({task}, timeout=HEARTBEAT_INTERVAL_S)
            if done:
                break
            yield _HEARTBEAT

        try:
            body = task.result()
        except Exception as exc:  # noqa: BLE001 — Rust's `unwrap_or_else(|_| ...)` is equally blind
            # THE WART (main.rs: `res.unwrap_or_else(|_| q.clone())`). The 200
            # is already committed, so a slow failure degrades to the untranslated
            # original text instead of an error status. `asyncio.CancelledError`
            # is a BaseException and deliberately NOT caught: a cancelled task
            # means we are being torn down, not that the LLM failed.
            chunk = _fallback_chunk(exc, fallback_body, on_fallback, time.monotonic() - started)
        else:
            chunk = _encode(body)
        yield chunk
    finally:
        # Client disconnected mid-stream (Litestar cancels the streaming task
        # group, which throws into this generator at its current await point,
        # or the generator is closed outright) -> abort the upstream call the
        # way dropping the Rust future does.
        if not task.done():
            task.cancel()


async def respond_with_grace(
    work: Coroutine[Any, Any, ResultBody],
    fallback_body: ResultBody,
    *,
    on_fallback: FallbackHook | None = None,
) -> Response[Any] | Stream:
    """Race ``work`` against the grace period; buffer if fast, stream if slow.

    Args:
        work: Coroutine producing the finished response body. Its exceptions
            are meaningful: on the fast path they propagate (so the app-level
            ``LlmError`` handler can render the Rust error body and status); on
            the slow path they are swallowed in favour of ``fallback_body``.
        fallback_body: The body to emit if a slow ``work`` fails — Rust builds
            it with the same ``translate_response_json`` the success path uses,
            passing the ORIGINAL text as the translation.
        on_fallback: Optional ``(exc, elapsed_s) -> body`` hook, called only
            when a slow ``work`` fails; its body is served instead of
            ``fallback_body``. If the hook raises, ``fallback_body`` is served
            (the ``200`` is already committed, nothing else is possible). Never
            called on the fast path, where the exception still propagates.

    Returns:
        A buffered :class:`~litestar.response.Response` (fast) or a chunked
        :class:`~litestar.response.Stream` (slow). Both are HTTP 200; the
        explicit status matters because Litestar would otherwise default a
        ``POST`` handler's response to 201.
    """
    started = time.monotonic()
    task: asyncio.Task[ResultBody] = asyncio.create_task(work)
    # consume terminal state; never log "Task exception was never retrieved"
    task.add_done_callback(lambda t: t.cancelled() or t.exception())
    try:
        done, _pending = await asyncio.wait({task}, timeout=GRACE_PERIOD_S)
    except BaseException:
        # The handler itself was cancelled (client vanished inside the grace
        # window, before anything was committed). Rust's `select!` drops `fut`
        # here; don't orphan the upstream call.
        task.cancel()
        raise

    if done:
        # `task.result()` re-raises the work coroutine's exception — that is the
        # fast-failure path, and it must stay an exception so the registered
        # handler turns it into the correct 4xx/5xx.
        return Response(
            content=task.result(),
            status_code=HTTP_200_OK,
            media_type=_JSON_MEDIA_TYPE,
        )

    return Stream(
        _heartbeat_stream(task, fallback_body, on_fallback, started),
        status_code=HTTP_200_OK,
        media_type=_JSON_MEDIA_TYPE,
    )
