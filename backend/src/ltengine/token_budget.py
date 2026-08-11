"""Output token budget — port of LTEngine's token_budget.rs + output_cap()."""
from __future__ import annotations

import math


def output_cap(input_chars: int, llm_max_tokens: int, chars_per_token: float,
               mult: float, floor: int) -> int | None:
    """Return the max_tokens value to send, or None to omit the field.

    Mirrors Rust: disabled entirely when llm_max_tokens == 0; dynamic budget only
    when chars_per_token AND mult are > 0; cap = ceil(ceil(chars/cpt) * mult),
    then raised to floor, then clamped to llm_max_tokens (ceiling wins over floor).
    """
    if llm_max_tokens <= 0:
        return None
    if chars_per_token > 0 and mult > 0:
        cap = math.ceil(math.ceil(input_chars / chars_per_token) * mult)
        cap = max(cap, floor)
        return min(cap, llm_max_tokens)
    return llm_max_tokens
