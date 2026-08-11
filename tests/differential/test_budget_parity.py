from .helpers import fire_both, last_payload

FLAGS = ["--llm-max-tokens", "16384", "--llm-chars-per-token", "3.0",
         "--llm-max-tokens-mult", "1.5", "--llm-max-tokens-floor", "64"]


def test_max_tokens_value_parity(make_pair) -> None:
    pair = make_pair(FLAGS)
    pair.reset()
    fire_both(pair, "POST", "/translate",
              json_body={"q": "x" * 1000, "source": "en", "target": "ru"})
    assert last_payload(pair.py) == last_payload(pair.rust)  # includes max_tokens=501


def test_max_tokens_absent_by_default(pair) -> None:
    pair.reset()
    fire_both(pair, "POST", "/translate",
              json_body={"q": "Hello", "source": "en", "target": "ru"})
    assert "max_tokens" not in last_payload(pair.py)
    assert "max_tokens" not in last_payload(pair.rust)
