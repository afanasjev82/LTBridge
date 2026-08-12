from .helpers import assert_parity, fire_both


def test_unknown_path_404_parity(pair) -> None:
    rust, py = fire_both(pair, "GET", "/no/such/thing.txt")
    assert_parity(rust, py)


def test_root_serves_html_status(pair) -> None:
    # Bodies differ by design (different UIs) — compare status + content-type only.
    rust, py = fire_both(pair, "GET", "/")
    assert py.status == rust.status == 200
    assert py.content_type == rust.content_type == "text/html"
