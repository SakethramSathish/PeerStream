"""Design system: tokens, palette, stylesheet and icons.

    from app.ui.theme import DARK, TOKENS, build_stylesheet, icon, palette_for

The stylesheet is generated from the palette and the tokens, so the colours
live in exactly one place and a hand-painted chart matches a styled widget
without anybody keeping them in sync by hand.
"""

from app.ui.theme.palette import DARK, LIGHT, Palette, palette_for
from app.ui.theme.qss import build_stylesheet
from app.ui.theme.tokens import TOKENS, Chart, Duration, Geometry, Radii, Spacing, Tokens, Type

from .icons import icon

__all__ = [
    "DARK",
    "LIGHT",
    "TOKENS",
    "Chart",
    "Duration",
    "Geometry",
    "Palette",
    "Radii",
    "Spacing",
    "Tokens",
    "Type",
    "build_stylesheet",
    "icon",
    "palette_for",
]
