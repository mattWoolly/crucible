"""Logo rendering and glitch-offset math (orchestrator/tui/logo.py)."""

import re

from orchestrator.tui.logo import (
    LOGO_GRADIENT,
    LOGO_SMALL,
    logo_lines,
    logo_markup,
)

_TAG = re.compile(r"\[/?[^\]]*\]")


def plain_lines(markup: str) -> list[str]:
    return [_TAG.sub("", ln) for ln in markup.splitlines()]


def test_logo_lines_are_uniform_width():
    lines = logo_lines()
    widths = {len(ln) for ln in lines}
    assert len(widths) == 1  # per-line centering must not stagger the block
    assert len(lines) == len(LOGO_GRADIENT)


def test_default_markup_is_aligned():
    lines = plain_lines(logo_markup())
    assert len({len(ln) for ln in lines}) == 1


def test_positive_offset_shifts_right_preserving_width():
    base = plain_lines(logo_markup())
    shifted = plain_lines(logo_markup(offsets=[3, 0, 0, 0, 0, 0]))
    assert shifted[0] == "   " + base[0][:-3]
    assert shifted[1:] == base[1:]
    assert len(shifted[0]) == len(base[0])


def test_negative_offset_shifts_left_preserving_width():
    base = plain_lines(logo_markup())
    shifted = plain_lines(logo_markup(offsets=[-2, 0, 0, 0, 0, 0]))
    assert shifted[0] == base[0][2:] + "  "
    assert len(shifted[0]) == len(base[0])


def test_color_override_applies_to_one_line_only():
    markup = logo_markup(colors={2: "#ffffff"})
    lines = markup.splitlines()
    assert lines[2].startswith("[bold #ffffff]")
    assert lines[0].startswith(f"[bold {LOGO_GRADIENT[0]}]")


def test_narrow_terminal_falls_back_to_small_mark():
    assert LOGO_SMALL in logo_markup(width=40)


def test_wide_terminal_gets_block_art():
    assert LOGO_SMALL not in logo_markup(width=200)
