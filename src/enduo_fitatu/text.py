"""Text that comes from Fitatu (names of foods, brands and meals) on its way to Enduo, where the coach, a language
model, reads it. It is cleaned and bounded, never interpreted: no control or invisible characters, plain whitespace,
and no more characters than a name plausibly needs. Pure functions only."""

from __future__ import annotations

import unicodedata
from typing import Any

#: What a reader does not see: control characters (Cc), invisible formatting characters (Cf: zero width, direction
#: overrides, tags) and halves of a surrogate pair (Cs), which cannot be encoded as UTF-8.
_INVISIBLE = frozenset({"Cc", "Cf", "Cs"})


def line(value: Any, limit: int) -> str | None:
    """A name or another text of one line, at most ``limit`` characters (a cut ends in an ellipsis); None when the
    value is no text or nothing visible is left of it."""
    if not isinstance(value, str):
        return None
    # Enough of the text to fill the limit after cleaning, so a huge field costs no more than a large one.
    kept = "".join(char for char in value[: limit * 8 + 64] if char.isspace() or unicodedata.category(char) not in _INVISIBLE)
    text = " ".join(kept.split())
    if len(text) > limit:
        text = text[: max(0, limit - 1)].rstrip() + "…"
    return text or None
