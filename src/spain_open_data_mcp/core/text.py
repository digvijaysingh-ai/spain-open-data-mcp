"""Cleaning free text that comes from upstream sources before it reaches the model.

OpenStreetMap tags, dataset titles and similar fields can be edited by anyone. They are
data, never instructions: strip control and formatting characters, collapse whitespace
(so a value cannot fake a new line or section in the tool output), and cap the length.
"""

from __future__ import annotations

import unicodedata

MAX_LEN = 80


def clean_text(value: str | None, max_len: int = MAX_LEN) -> str | None:
    """A single line of printable text, at most `max_len` characters, or None if nothing is left."""
    if not value:
        return None
    kept = "".join(" " if unicodedata.category(ch).startswith(("C", "Z")) else ch for ch in value)
    text = " ".join(kept.split())
    if len(text) > max_len:
        text = text[: max_len - 1].rstrip() + "…"
    return text or None


def quoted(value: str) -> str:
    """Show an upstream string as quoted data in text output."""
    return '"' + value.replace('"', "'") + '"'
