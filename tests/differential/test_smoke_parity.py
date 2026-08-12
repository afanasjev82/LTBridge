from .helpers import assert_parity, fire_both


def test_languages_parity(pair) -> None:
    rust, py = fire_both(pair, "GET", "/languages")
    assert rust.status == 200
    assert_parity(rust, py)


def test_translate_happy_path_parity(pair) -> None:
    pair.reset()
    rust, py = fire_both(pair, "POST", "/translate",
                         json_body={"q": "Hello", "source": "en", "target": "ru"})
    assert rust.status == 200
    assert_parity(rust, py)


def test_health_get_is_truly_empty_no_content_type(pair) -> None:
    # The Medusa V2 program spec's LTEngine api-mode inventory pins Rust /health
    # as "200, EMPTY body" (this repo's own P1 spec doesn't restate it) --
    # oracle-verified GET /health -> 200, Content-Length: 0, body b"", and NO
    # content-type header at all (main.rs's handler is a bare
    # `HttpResponse::Ok().finish()`).
    rust, py = fire_both(pair, "GET", "/health")
    assert rust.status == 200
    assert rust.body == b""
    assert not rust.content_type_present
    assert_parity(rust, py)


def test_health_head_gets_pinned_404(pair) -> None:
    # /health is GET-only, so HEAD falls through the same CRITICAL-2 path as
    # any other GET-only route (e.g. /languages): the oracle's embedded
    # static-file service allows HEAD through its method guard, then misses
    # on the file lookup -- 404, not 200 and not 405. Oracle-verified.
    rust, py = fire_both(pair, "HEAD", "/health")
    assert rust.status == 404
    assert rust.body == b""
    assert_parity(rust, py)
