"""Contrast: whether one colour on another can actually be read.

Accessibility is not a taste question and it should not be settled by eye, so
this module turns it into arithmetic. WCAG 2.1 gives the formula — relative
luminance, then a ratio between 1:1 and 21:1 — and the tests in
``tests/ui/test_accessibility.py`` hold the palette to it. A role that fails is
a bug in the theme, the same way a message that fails to decode is a bug in the
codec.

The thresholds, and what each one is for:

* **4.5:1** — normal text. Anything a user is meant to read as words.
* **3:1** — large text (18 pt, or 14 pt bold), and *non-text* contrast: the
  boundary of a control, the fill of a chart, the colour that distinguishes one
  state from another (WCAG 1.4.11). A piece matrix whose "missing" colour is
  indistinguishable from its background is not showing five states, it is
  showing four.
* **7:1** — AAA, for text a user has to read for a long time.

Colours are written the way Qt reads them, ``#RRGGBB`` or ``#AARRGGBB``;
translucent roles are composited over their background before being measured,
because that is what the eye is actually looking at.
"""

from __future__ import annotations

from typing import Final

WCAG_AA_NORMAL: Final[float] = 4.5
"""Body text: the ratio below which words become work to read."""

WCAG_AA_LARGE: Final[float] = 3.0
"""Large text, and every non-text boundary, state or fill."""

WCAG_AAA_NORMAL: Final[float] = 7.0
"""Enhanced contrast, for long-form reading."""

WCAG_NON_TEXT: Final[float] = 3.0
"""Non-text contrast (WCAG 1.4.11): states, fills, control boundaries."""

_MIN_CHANNELS: Final[int] = 6


def _channel_to_linear(value: int) -> float:
    """Undo sRGB gamma for one 0-255 channel, giving 0.0-1.0 linear light."""
    normalised = value / 255
    if normalised <= 0.04045:
        return normalised / 12.92
    return float(((normalised + 0.055) / 1.055) ** 2.4)


def _parse(colour: str) -> tuple[int, int, int, int]:
    """Split a Qt hex colour into ``(red, green, blue, alpha)``.

    Raises:
        ValueError: If it is not 6 or 8 hex digits — a colour we cannot parse
            is a colour we must not silently treat as black.
    """
    raw = colour.strip().lstrip("#")
    if len(raw) not in {6, 8} or any(
        character not in "0123456789abcdefABCDEF" for character in raw
    ):
        raise ValueError(f"colours must be #RRGGBB or #AARRGGBB, got {colour!r}")
    if len(raw) == 6:
        raw = "FF" + raw
    return (
        int(raw[2:4], 16),
        int(raw[4:6], 16),
        int(raw[6:8], 16),
        int(raw[0:2], 16),
    )


def relative_luminance(colour: str) -> float:
    """Perceived brightness of a colour, 0.0 (black) to 1.0 (white).

    Weighted the way the eye is: green carries most of the brightness a human
    sees, blue almost none of it.
    """
    red, green, blue, _alpha = _parse(colour)
    return (
        0.2126 * _channel_to_linear(red)
        + 0.7152 * _channel_to_linear(green)
        + 0.0722 * _channel_to_linear(blue)
    )


def composite(foreground: str, background: str) -> str:
    """Flatten a translucent colour over an opaque one, as ``#RRGGBB``.

    An eight-digit role is a promise about how it will be drawn: over whatever
    is behind it. Measuring ``accent_soft`` against a background without
    blending it first would be measuring a colour nobody ever sees.
    """
    red, green, blue, alpha = _parse(foreground)
    back_red, back_green, back_blue, _ = _parse(background)
    weight = alpha / 255
    mixed = tuple(
        round(channel * weight + behind * (1 - weight))
        for channel, behind in ((red, back_red), (green, back_green), (blue, back_blue))
    )
    return "#{:02X}{:02X}{:02X}".format(*mixed)


def contrast_ratio(foreground: str, background: str) -> float:
    """How far apart two colours are, 1.0 (identical) to 21.0 (black on white).

    Translucent foregrounds are composited over the background first.
    """
    if len(foreground.strip().lstrip("#")) == 8:
        foreground = composite(foreground, background)
    lighter = max(relative_luminance(foreground), relative_luminance(background))
    darker = min(relative_luminance(foreground), relative_luminance(background))
    return (lighter + 0.05) / (darker + 0.05)


def meets(foreground: str, background: str, *, level: float = WCAG_AA_NORMAL) -> bool:
    """Whether the pair reaches ``level``, e.g. :data:`WCAG_AA_NORMAL`."""
    return contrast_ratio(foreground, background) >= level


def describe(ratio: float) -> str:
    """Name what a ratio is good for, e.g. ``"AA (normal text)"``.

    Used by the tests' failure messages: a number alone does not tell the next
    person whether 3.4 is a near miss or a mile out.
    """
    if ratio >= WCAG_AAA_NORMAL:
        return "AAA (normal text)"
    if ratio >= WCAG_AA_NORMAL:
        return "AA (normal text)"
    if ratio >= WCAG_AA_LARGE:
        return "AA large text and non-text only"
    return "fails every level"
