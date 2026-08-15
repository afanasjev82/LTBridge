"""Translation output post-processing.

Extracted from app.py's ``_improve_formatting`` (pure cut-paste, renamed to
drop the leading underscore now that it lives in its own public module).
"""

from __future__ import annotations

_PUNCTUATION = set("!?.,;。")


def improve_formatting(source: str, translation: str) -> str:
    """Post-process translation output to align punctuation and case with source.

    Direct port of improve_formatting() in LTEngine's main.rs.
    """
    t = translation.strip()

    if not source:
        return ""
    if not t:
        return source

    q_last = source[-1]
    t_last = t[-1]

    if q_last in _PUNCTUATION:
        if q_last != t_last:
            if t_last in _PUNCTUATION:
                t = t[:-1]
            t += q_last
    elif t_last in _PUNCTUATION:
        t = t[:-1]

    if source.islower():
        t = t.lower()
    elif source.isupper():
        t = t.upper()
    elif source and t:
        if source[0].islower() and t[0].isupper():
            t = t[0].lower() + t[1:]
        elif source[0].isupper() and t[0].islower():
            t = t[0].upper() + t[1:]

    return t.strip()
