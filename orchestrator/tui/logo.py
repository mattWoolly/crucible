"""The Crucible splash logo (pure data — no Textual imports)."""

from __future__ import annotations

# ANSI-shadow block letters, 62 columns wide.
LOGO = """\
  ██████╗██████╗ ██╗   ██╗ ██████╗██╗██████╗ ██╗     ███████╗
 ██╔════╝██╔══██╗██║   ██║██╔════╝██║██╔══██╗██║     ██╔════╝
 ██║     ██████╔╝██║   ██║██║     ██║██████╔╝██║     █████╗
 ██║     ██╔══██╗██║   ██║██║     ██║██╔══██╗██║     ██╔══╝
 ╚██████╗██║  ██║╚██████╔╝╚██████╗██║██████╔╝███████╗███████╗
  ╚═════╝╚═╝  ╚═╝ ╚═════╝  ╚═════╝╚═╝╚═════╝ ╚══════╝╚══════╝"""

# Fallback for terminals too narrow for the block art.
LOGO_SMALL = "◆ C R U C I B L E ◆"

TAGLINE = "small models · real software · verified green"

# One color per logo line, hot-metal gradient top→bottom (a crucible, after
# all). Rendered as Rich markup colors by the splash screen.
LOGO_GRADIENT = ["#ffd54a", "#ffb03a", "#ff8c2e", "#f96a25", "#ef4e20", "#d93a1e"]


def logo_lines() -> list[str]:
    """The logo lines padded to uniform width — REQUIRED for alignment: the
    splash centers per line, so ragged line lengths visibly stagger the block."""
    lines = LOGO.splitlines()
    width = max(len(ln) for ln in lines)
    return [ln.ljust(width) for ln in lines]


def logo_markup(
    width: int | None = None,
    offsets: list[int] | None = None,
    colors: dict[int, str] | None = None,
) -> str:
    """The logo as Rich console markup, gradient-tinted.

    ``offsets`` shifts each line horizontally (chars; +right / -left) while
    preserving line width — the splash's glitch animation. ``colors``
    overrides the gradient for individual lines (flicker frames). Falls back
    to the small mark when the terminal is narrower than the block art.
    """
    lines = logo_lines()
    if width is not None and width < len(lines[0]):
        return f"[bold #ff8c2e]{LOGO_SMALL}[/]"
    out = []
    for i, (line, color) in enumerate(zip(lines, LOGO_GRADIENT)):
        o = offsets[i] if offsets else 0
        if o > 0:
            line = " " * o + line[:-o] if o < len(line) else " " * len(line)
        elif o < 0:
            line = line[-o:] + " " * (-o)
        c = colors.get(i, color) if colors else color
        out.append(f"[bold {c}]{line}[/]")
    return "\n".join(out)
