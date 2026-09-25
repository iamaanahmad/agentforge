"""Public presentation only; never serialize application settings into HTML."""

import html
import re
from pathlib import Path


def render_brand(source: str, settings) -> str:
    values = {
        "BRAND_NAME": settings.brand_name,
        "BRAND_TAGLINE": settings.brand_tagline,
        "BRAND_ACCENT": settings.brand_accent,
        "BRAND_LOGO": "/brand/logo" if settings.brand_logo_file else "/static/mark.svg",
    }
    return re.sub(r"\{\{(BRAND_[A-Z]+)\}\}", lambda m: html.escape(values[m[1]], quote=True), source)


def load_logo(path: Path | None):
    if path is None:
        return None
    with path.open("rb") as stream:
        data = stream.read(262145)
    if not data or len(data) > 262144:
        raise ValueError("Brand logo must be at most 256 KiB")
    if data.startswith(b"\x89PNG\r\n\x1a\n"):
        media = "image/png"
    elif data.startswith(b"\xff\xd8\xff"):
        media = "image/jpeg"
    elif data.startswith(b"RIFF") and data[8:12] == b"WEBP":
        media = "image/webp"
    else:
        raise ValueError("Brand logo must be PNG, JPEG, or WebP; SVG and HTML are not accepted")
    return data, media
