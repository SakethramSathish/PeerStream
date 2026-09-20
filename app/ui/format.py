"""Humanising numbers: the only place in the UI that formats a value.

Every panel needs the same handful of shapes — bytes, rates, durations,
percentages — and if each one invents its own, the same torrent shows three
different sizes depending on which tab you are looking at. So they live here,
and the rules are stated once:

* **Bytes are binary.** 1 KiB is 1024 bytes, because that is what the piece
  maths uses and a 6 % discrepancy between two panels is worse than pedantry.
* **Rates are bytes per second, three significant figures.** Enough to watch a
  number move, not so much that it flickers unreadably.
* **A duration that cannot be known is not zero.** An unknown ETA is ``"--"``,
  because "0s" reads as "any moment now" and that is a claim about the future
  the client has not earned.
* **A percentage is a fraction of the whole, not of the part.** Progress is
  verified bytes over total bytes.
"""

from __future__ import annotations

# Binary units, in order. Deliberately not SI: the protocol counts in powers
# of two and the interface should agree with it.
_UNITS: tuple[str, ...] = ("B", "KiB", "MiB", "GiB", "TiB", "PiB")

UNKNOWN: str = "--"
"""What is shown when a number has not been measured."""


def human_bytes(value: float | None, *, suffix: str = "") -> str:
    """Bytes, as ``"12.4 MiB"``.

    Args:
        value: A byte count. ``None`` means "not measured".
        suffix: An optional trailing unit, e.g. ``"/s"`` for a rate.

    Returns:
        The formatted string, or :data:`UNKNOWN` when there is no value.
    """
    if value is None:
        return UNKNOWN
    amount = float(value)
    if amount < 0:
        return UNKNOWN
    for unit in _UNITS:
        if amount < 1024.0:
            text = f"{round(amount)} {unit}" if unit == "B" else f"{amount:.2f} {unit}"
            return text + suffix
        amount /= 1024.0
    return f"{amount:.2f} {_UNITS[-1]}{suffix}"


def human_rate(value: float | None) -> str:
    """Bytes per second, as ``"1.24 MiB/s"``.

    Args:
        value: Bytes per second. ``None`` or ``0`` reads as ``"0 B/s"``: a
            measured zero is different from an unknown one.
    """
    if value is None:
        return UNKNOWN
    if value <= 0.0:
        return "0 B/s"
    return human_bytes(value, suffix="/s")


def human_duration(seconds: float | None) -> str:
    """A length of time, as ``"4m 12s"``.

    Args:
        seconds: A duration. ``None`` (or negative) becomes :data:`UNKNOWN`,
            because an ETA that is unknown is not an ETA of zero.
    """
    if seconds is None or seconds < 0:
        return UNKNOWN
    total = round(seconds)
    if total < 60:
        return f"{total}s"
    if total < 3600:
        return f"{total // 60}m {total % 60:02d}s"
    if total < 86400:
        return f"{total // 3600}h {(total % 3600) // 60:02d}m"
    return f"{total // 86400}d {(total % 86400) // 3600:02d}h"


def human_percent(fraction: float | None, *, places: int = 1) -> str:
    """A fraction of 1.0, as ``"63.4%"``.

    Args:
        fraction: ``0.0``-``1.0``; values outside are clamped, since a
            percentage above 100 is a bug worth hiding, not worth showing.
        places: Decimal places.
    """
    if fraction is None:
        return UNKNOWN
    clamped = min(1.0, max(0.0, float(fraction)))
    return f"{clamped * 100:.{places}f}%"


def human_ratio(ratio: float | None) -> str:
    """A share ratio, as ``"1.42"``, or :data:`UNKNOWN` before anything is known."""
    if ratio is None:
        return UNKNOWN
    return f"{ratio:.2f}"


def human_count(value: int | None) -> str:
    """A count with thousands separators, or :data:`UNKNOWN`."""
    if value is None:
        return UNKNOWN
    return f"{int(value):,}"


__all__ = [
    "UNKNOWN",
    "human_bytes",
    "human_count",
    "human_duration",
    "human_percent",
    "human_rate",
    "human_ratio",
]
