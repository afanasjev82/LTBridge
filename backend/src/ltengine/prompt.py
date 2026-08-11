"""Translation prompt builder.

Port of LTEngine's prompt.rs — produces the same system/user prompt pair
that the Rust implementation sends to the LLM.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass
class Prompt:
    system: str
    user: str


_SYSTEM_TEXT = (
    "You are an expert linguist, specializing in translation. "
    "You are able to capture the nuances of the languages you translate. "
    "You pay attention to masculine/feminine/plural and proper use of articles and grammar. "
    "You always provide natural sounding translations that fully preserve the meaning of the original text. "
    "You never provide explanations for your work. "
    "You always answer with the translated text and nothing else."
)

_SYSTEM_HTML = (
    "You are an expert linguist, specializing in translation. "
    "You are able to capture the nuances of the languages you translate. "
    "You pay attention to masculine/feminine/plural and proper use of articles and grammar. "
    "You always provide natural sounding translations that fully preserve the meaning of the original text. "
    "You never provide explanations for your work. "
    "You must preserve all HTML tags and elements in the translation. "
    "You always answer with the translated text and nothing else."
)


class PromptBuilder:
    """Builds system+user prompts for translation (mirrors LTEngine's PromptBuilder)."""

    def __init__(self) -> None:
        self._source: str = "auto"
        self._target: str = "English"
        self._format: str = "text"

    def set_source_language(self, name: str) -> "PromptBuilder":
        self._source = name
        return self

    def set_target_language(self, name: str) -> "PromptBuilder":
        self._target = name
        return self

    def set_format(self, fmt: str) -> "PromptBuilder":
        self._format = fmt
        return self

    def build(self, text: str) -> Prompt:
        system = _SYSTEM_HTML if self._format == "html" else _SYSTEM_TEXT

        if self._source == "auto":
            user = (
                f"Translate the text below to {self._target}.\n\n"
                f"Text: {text}\n\n"
                f"{self._target}:\n"
            )
        else:
            user = (
                f"Translate the text below from {self._source} to {self._target}.\n\n"
                f"{self._source}: {text}\n\n"
                f"{self._target}:\n"
            )

        return Prompt(system=system, user=user)
