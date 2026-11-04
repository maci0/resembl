"""Compatibility shim: the palettes now live in ``resembl.theme``.

``from resembl.status_colors import RICH_STYLES`` / ``LIGHT`` / ``TERMINAL``
keeps working; in-tree code imports from ``resembl.theme`` directly.
"""

from __future__ import annotations

from .theme import LIGHT, RICH_STYLES, TERMINAL

__all__ = ["LIGHT", "RICH_STYLES", "TERMINAL"]
