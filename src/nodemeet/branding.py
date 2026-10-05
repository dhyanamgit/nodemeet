"""White-label branding for the meeting UI, booking pages, embeds and emails.

Layers (later wins): library defaults -> ``NodeMeet(branding=...)`` ->
tenant branding (``PUT /api/branding``) -> room branding (``RoomConfig.branding``).
Every field is optional; you only set what you want to change.
"""
from __future__ import annotations

import copy
import html
import json
import re
from typing import Any, Dict, Mapping, Optional

DEFAULT_TOOLBAR = ["mic", "camera", "screen", "hand", "reactions", "chat", "people",
                   "whiteboard", "captions", "layout", "settings", "more", "leave"]

DEFAULTS: Dict[str, Any] = {
    "name": "nodemeet",                 # product name shown in titles, emails, footer
    "logo_url": "",                     # header logo (light backgrounds)
    "logo_dark_url": "",                # logo for dark backgrounds
    "favicon_url": "",
    "show_powered_by": True,
    "theme": "dark",                    # dark | light | auto
    "colors": {                         # dark theme palette
        "primary": "#6366f1", "primary_text": "#ffffff", "background": "#0f1115",
        "surface": "#181b22", "surface_2": "#232733", "tile": "#232733", "border": "#262b36",
        "text": "#eef0f4", "muted": "#9aa3b2", "danger": "#ef4444", "success": "#22c55e",
        "warning": "#f59e0b", "focus": "#a5b4fc", "overlay": "rgba(0,0,0,.55)",
    },
    "light_colors": {                   # used when theme is light (or auto + light OS)
        "primary": "#4f46e5", "primary_text": "#ffffff", "background": "#f5f6fa",
        "surface": "#ffffff", "surface_2": "#eef0f5", "tile": "#e5e7eb", "border": "#e5e7eb",
        "text": "#111827", "muted": "#6b7280", "danger": "#dc2626", "success": "#16a34a",
        "warning": "#d97706", "focus": "#6366f1", "overlay": "rgba(255,255,255,.75)",
    },
    "font_family": "system-ui, -apple-system, Segoe UI, Roboto, sans-serif",
    "heading_font_family": "",
    "font_url": "",                     # stylesheet URL, e.g. a self-hosted @font-face or Google Fonts
    "font_size": 14,
    "radius": 12,                       # tiles/cards
    "button_radius": 999,               # 999 = pill buttons
    "tile_gap": 8,
    "tile_aspect": "16/9",
    "button_style": "filled",           # filled | outline | ghost
    "background_image_url": "",         # behind the pre-join / waiting / end screens
    "page_title": "{room} · {name}",    # {room} {name} placeholders
    "prejoin_title": "",
    "prejoin_subtitle": "",
    "lobby_message": "",
    "end_message": "",
    "leave_redirect_url": "",           # send people here after leaving
    "language": "en",
    "strings": {},                      # override any UI text, see nodemeet-ui.js STRINGS
    "toolbar": list(DEFAULT_TOOLBAR),   # order + which buttons exist
    "features": {
        "prejoin": True, "device_preview": True, "chat": True, "people": True, "reactions": True,
        "settings": True, "layout_switch": True, "fullscreen": True, "private_chat": True,
        "notifications": True, "sounds": True, "show_names": True, "show_role_badges": True,
        "self_view": True, "picture_in_picture": True, "polls": True, "qa": True,
        "whiteboard": True, "breakouts": True, "captions": True, "recording": True,
    },
    "default_layout": "grid",           # grid | speaker | sidebar
    "reactions": ["👍", "👏", "😂", "❤️", "🎉", "😮", "🙌", "🤔"],
    "custom_css": "",                   # appended after the theme
    "custom_css_url": "",
    "custom_head_html": "",             # meta tags, analytics... (self-hosted: your call)
    "meta": {},                         # extra <meta name=... content=...>
    "email": {"logo_url": "", "accent": "", "footer": "", "from_name": ""},
    "booking": {"title": "", "subtitle": "", "confirm_text": "", "accent": ""},
}

_COLOR_RE = re.compile(r"^(#[0-9a-fA-F]{3,8}|rgba?\([\d\s.,%]+\)|hsla?\([\d\s.,%deg]+\)|[a-zA-Z]{3,20})$")


def deep_merge(base: Mapping[str, Any], override: Optional[Mapping[str, Any]]) -> Dict[str, Any]:
    out = copy.deepcopy(dict(base))
    for key, value in (override or {}).items():
        if isinstance(value, Mapping) and isinstance(out.get(key), Mapping):
            out[key] = deep_merge(out[key], value)
        elif value is not None:
            out[key] = copy.deepcopy(value)
    return out


def validate(branding: Mapping[str, Any]) -> Dict[str, Any]:
    """Reject unknown top-level keys and unsafe colour values (CSS injection)."""
    data = dict(branding or {})
    unknown = set(data) - set(DEFAULTS)
    if unknown:
        raise ValueError(f"unknown branding keys: {sorted(unknown)}")
    for group in ("colors", "light_colors"):
        for k, v in (data.get(group) or {}).items():
            if not _COLOR_RE.match(str(v).strip()):
                raise ValueError(f"{group}.{k}: {v!r} is not a CSS colour")
    for key in ("font_family", "heading_font_family", "tile_aspect"):
        if key in data and re.search(r"[;{}<>]", str(data[key])):
            raise ValueError(f"{key} contains forbidden characters")
    for key in ("radius", "button_radius", "tile_gap", "font_size"):
        if key in data and not isinstance(data[key], (int, float)):
            raise ValueError(f"{key} must be a number")
    for bad in set(data.get("toolbar") or []) - set(DEFAULT_TOOLBAR):
        raise ValueError(f"unknown toolbar button {bad!r}; choose from {DEFAULT_TOOLBAR}")
    if data.get("theme") not in (None, "dark", "light", "auto"):
        raise ValueError("theme must be dark, light or auto")
    return data


def _vars(colors: Mapping[str, Any]) -> str:
    return ";".join(f"--nm-{k.replace('_', '-')}:{v}" for k, v in colors.items())


def css(b: Mapping[str, Any]) -> str:
    """CSS variables + custom CSS for a resolved branding dict."""
    font = b.get("font_family") or DEFAULTS["font_family"]
    base = (f"--nm-font:{font};--nm-heading-font:{b.get('heading_font_family') or font};"
            f"--nm-font-size:{b.get('font_size', 14)}px;--nm-radius:{b.get('radius', 12)}px;"
            f"--nm-button-radius:{b.get('button_radius', 999)}px;--nm-gap:{b.get('tile_gap', 8)}px;"
            f"--nm-aspect:{b.get('tile_aspect', '16/9')}")
    bg = b.get("background_image_url")
    if bg:
        base += f";--nm-bg-image:url({json.dumps(str(bg))})"
    dark, light = _vars(b.get("colors") or {}), _vars(b.get("light_colors") or {})
    theme = b.get("theme", "dark")
    if theme == "light":
        out = f":root{{{base};{light}}}"
    elif theme == "auto":
        out = (f":root{{{base};{dark}}}"
               f"@media (prefers-color-scheme: light){{:root{{{light}}}}}")
    else:
        out = f":root{{{base};{dark}}}"
    custom = str(b.get("custom_css") or "").replace("</style", "<\\/style")
    return out + "\n" + custom


def head_html(b: Mapping[str, Any]) -> str:
    """<link>/<meta> tags for the <head> of every page."""
    e = lambda v: html.escape(str(v), quote=True)  # noqa: E731
    parts = []
    if b.get("favicon_url"):
        parts.append(f'<link rel="icon" href="{e(b["favicon_url"])}">')
    if b.get("font_url"):
        parts.append(f'<link rel="stylesheet" href="{e(b["font_url"])}">')
    if b.get("custom_css_url"):
        parts.append(f'<link rel="stylesheet" href="{e(b["custom_css_url"])}">')
    for name, content in (b.get("meta") or {}).items():
        parts.append(f'<meta name="{e(name)}" content="{e(content)}">')
    parts.append(f"<style>{css(b)}</style>")
    if b.get("custom_head_html"):
        parts.append(str(b["custom_head_html"]))
    return "\n".join(parts)


def public(b: Mapping[str, Any]) -> Dict[str, Any]:
    """What the browser needs (everything except raw head HTML)."""
    return {k: v for k, v in b.items() if k != "custom_head_html"}


class BrandingResolver:
    def __init__(self, base: Optional[Mapping[str, Any]] = None, storage: Any = None) -> None:
        self.base = deep_merge(DEFAULTS, validate(base or {}))
        self.storage = storage

    async def tenant(self, tenant_id: Optional[str]) -> Dict[str, Any]:
        try:
            stored = await self.storage.get_branding(tenant_id or "_default") if self.storage else None
        except NotImplementedError:
            stored = None
        return stored or {}

    async def resolve(self, *, tenant_id: Optional[str] = None,
                      room_branding: Optional[Mapping[str, Any]] = None) -> Dict[str, Any]:
        b = deep_merge(self.base, await self.tenant(None))
        if tenant_id:
            b = deep_merge(b, await self.tenant(tenant_id))
        return deep_merge(b, room_branding or {})
