import pytest

from .helpers import assert_parity, fire_both

TEXTS = [
    "Hello, how are you today?",
    "Привет, как дела?",
    "Bonjour tout le monde",
    "Hallo, wie geht es dir?",
    "Hola, ¿cómo estás?",
    "Ciao, come stai?",
    "こんにちは、お元気ですか",
    "안녕하세요",
    "你好，世界",
    "مرحبا بالعالم",
    "Tere, kuidas läheb?",
    "Cześć, jak się masz?",
    "Merhaba dünya",
    "Xin chào thế giới",
    "สวัสดีครับ",
    "Γειά σου κόσμε",
    "שלום עולם",
    "Hej, hur mår du?",
    "Ahoj, jak se máš?",
    "Salut, ce mai faci?",
    "ok",
    "123",
    "a",
    "",
    " ",  # short/ambiguous/degenerate
]


@pytest.mark.parametrize("text", TEXTS, ids=[t[:12] or "empty" for t in TEXTS])
def test_detect_parity(pair, text) -> None:
    rust, py = fire_both(pair, "POST", "/detect", json_body={"q": text})
    assert_parity(rust, py)


def test_translate_auto_detected_language_parity(pair) -> None:
    pair.reset()
    rust, py = fire_both(
        pair, "POST", "/translate", json_body={"q": "Привет, мир", "source": "auto", "target": "en"}
    )
    assert_parity(rust, py)
