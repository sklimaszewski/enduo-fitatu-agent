from __future__ import annotations

from enduo_fitatu.text import line


def test_removes_control_and_invisible_characters_and_collapses_whitespace() -> None:
    assert line("  Jogurt\tnaturalny\r\n(2%)\x00\x1b[31m ", 100) == "Jogurt naturalny (2%)[31m"
    assert line("Ser\u200b\u200e bia\u0142y\u202e\u2066 \u00a0 chudy\ufeff", 100) == "Ser bia\u0142y chudy"
    assert line("a\u2028b\u2029c\x85d", 100) == "a b c d"
    assert line("Sok" + chr(0xE0041) + chr(0xE0042), 100) == "Sok"


def test_drops_half_a_surrogate_pair_which_would_not_encode() -> None:
    assert line("Banan \ud83d", 100) == "Banan"
    assert line("Banan \U0001f34c", 100) == "Banan \U0001f34c"


def test_cuts_a_long_text_with_an_ellipsis() -> None:
    assert line("abcdefghij", 10) == "abcdefghij"
    assert line("abcdefghijk", 10) == "abcdefghi\u2026"
    assert len(line("x" * 1_000_000, 120) or "") == 120


def test_gives_nothing_without_a_visible_character_or_for_what_is_no_text() -> None:
    assert line(" \x07\u200b\n ", 10) is None
    assert line(None, 10) is None
    assert line(42, 10) is None
    assert line(["Ry\u017c"], 10) is None
