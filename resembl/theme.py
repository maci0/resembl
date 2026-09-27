"""The output palette, in one place.

Color carries exactly one of two roles.

* **Structure** (table titles, panel borders) wears :data:`ACCENT`, the
  brand green sampled from the shipped icon, mascot, and favicon
  (``docs/resembl_icon.png``), so the terminal and the project's assets
  read as one thing.
* **Status** (errors, warnings, score tiers) uses rich's native semantic
  names, so a user's own terminal theme still decides how those read.

A value is never colored for decoration: the score is the one number in
``find`` output that earns a color, and it gets one because its value is
a judgment, not because the row needs a hue.
"""

from __future__ import annotations

#: Brand green, sampled from ``docs/resembl_icon.png``.
ACCENT = "#10911A"

#: A hybrid score at or above this is a strong match.
SCORE_STRONG = 80.0

#: A hybrid score at or above this is a weak match; below it, a near miss.
SCORE_WEAK = 50.0


def score_color(score: float) -> str:
    """Return the status color for a 0-100 hybrid *score*."""
    if score >= SCORE_STRONG:
        return "green"
    if score >= SCORE_WEAK:
        return "yellow"
    return "red"
