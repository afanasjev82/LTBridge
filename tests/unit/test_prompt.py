from ltbridge.core.prompt import PromptBuilder

SYSTEM_TEXT = (
    "You are an expert linguist, specializing in translation. "
    "You are able to capture the nuances of the languages you translate. "
    "You pay attention to masculine/feminine/plural and proper use of articles and grammar. "
    "You always provide natural sounding translations that fully preserve the meaning of the original text. "
    "You never provide explanations for your work. "
    "You always answer with the translated text and nothing else."
)


def test_explicit_source_user_prompt() -> None:
    p = PromptBuilder().set_source_language("English").set_target_language("Russian").set_format("text").build("Hello")
    assert p.system == SYSTEM_TEXT
    assert p.user == "Translate the text below from English to Russian.\n\nEnglish: Hello\n\nRussian:\n"


def test_auto_source_user_prompt() -> None:
    p = PromptBuilder().set_source_language("auto").set_target_language("German").build("Hi")
    assert p.user == "Translate the text below to German.\n\nText: Hi\n\nGerman:\n"


def test_html_system_prompt_has_tag_sentence() -> None:
    p = PromptBuilder().set_format("html").set_target_language("French").build("<b>x</b>")
    assert "You must preserve all HTML tags and elements in the translation. " in p.system
    assert p.system.endswith("You always answer with the translated text and nothing else.")
