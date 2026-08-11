from .helpers import fire_both, last_payload


def test_chat_payload_identical(pair) -> None:
    pair.reset()
    fire_both(pair, "POST", "/translate",
              json_body={"q": "Hello", "source": "en", "target": "ru"})
    rust_payload = last_payload(pair.rust)
    py_payload = last_payload(pair.py)
    assert py_payload == rust_payload
