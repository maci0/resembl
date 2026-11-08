"""The output palette, in one place.

Color carries exactly one of two roles.

* **Structure** (table titles, panel borders) wears :data:`ACCENT`, the
  brand green sampled from the shipped icon, mascot, and favicon
  (``docs/resembl_icon.png``), so the terminal and the project's assets
  read as one thing.
* **Status** (errors, warnings, score tiers) uses the shared Relumea role
  palette through the CLI Rich theme. No-color modes and machine output
  remain plain; limited-color terminals receive the nearest available color.

A value is never colored for decoration: the score is the one number in
``find`` output that earns a color, and it gets one because its value is
a judgment, not because the row needs a hue.

The role palettes below are the shared Relumea brand values (LIGHT mirrors
the web word tokens; TERMINAL balances black and white grounds), the same
ones ``status_colors.py`` previously carried separately; they live here
now so the CLI's presentation concerns sit in one module.
"""

from __future__ import annotations

#: Brand green, sampled from ``docs/resembl_icon.png``.
ACCENT = "#10911A"

#: A hybrid score at or above this is a strong match.
SCORE_STRONG = 80.0

#: A hybrid score at or above this is a weak match; below it, a near miss.
SCORE_WEAK = 50.0

#: Shared Relumea role palette for light grounds.
LIGHT: dict[str, str] = {
    "exact": "#0f7a42",
    "reloc": "#00686e",
    "proven": "#265937",
    "library": "#943a77",
    "thunk": "#5b3fa8",
    "near": "#445b99",
    "warn": "#8a5a00",
    "stub": "#5f6673",
    "live": "#0b5c8a",
    "fail": "#a32318",
}

#: Shared Relumea role palette for terminal grounds.
TERMINAL: dict[str, str] = {
    "exact": "#1e864f",
    "reloc": "#02828b",
    "proven": "#547f67",
    "library": "#a65d89",
    "thunk": "#7c68be",
    "near": "#6173ac",
    "stub": "#717581",
    "live": "#397ba8",
    "fail": "#bf544c",
    "warn": "#9a6d20",
}

#: Rich theme mapping the stock style names onto the TERMINAL palette,
#: plus both bold spellings of each.
RICH_STYLES: dict[str, str] = {
    name: TERMINAL[role]
    for name, role in (
        ("green", "exact"),
        ("cyan", "live"),
        ("blue", "near"),
        ("magenta", "thunk"),
        ("yellow", "warn"),
        ("red", "fail"),
    )
}
RICH_STYLES.update({f"bold {name}": f"bold {value}" for name, value in list(RICH_STYLES.items())})
RICH_STYLES.update(
    {
        f"{name} bold": f"bold {value}"
        for name, value in list(RICH_STYLES.items())
        if " " not in name
    }
)


def score_color(score: float) -> str:
    """Return the status color for a 0-100 hybrid *score*."""
    if score >= SCORE_STRONG:
        return "green"
    if score >= SCORE_WEAK:
        return "yellow"
    return "red"
