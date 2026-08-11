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
