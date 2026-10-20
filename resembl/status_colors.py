"""Shared status colors, owned by relumea-brand and copied with provenance.

LIGHT mirrors the web word tokens; TERMINAL balances black and white grounds.
Keep verdicts, origin, activity and warnings semantically separate.
"""

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

RICH_STYLES: dict[str, str] = {
    "green": TERMINAL["exact"],
    "cyan": TERMINAL["live"],
    "blue": TERMINAL["near"],
    "magenta": TERMINAL["thunk"],
    "yellow": TERMINAL["warn"],
    "red": TERMINAL["fail"],
}

# Rich resolves combined style strings separately from their plain color name.
for name, color in tuple(RICH_STYLES.items()):
    RICH_STYLES[f"bold {name}"] = f"bold {color}"
    RICH_STYLES[f"{name} bold"] = f"bold {color}"
