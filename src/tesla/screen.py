"""What Vuk is looking at: active window, recent selection and a screenshot.

The GNOME Shell extension captures this at the moment the hotkey is pressed
(before the notch appears) and passes it to `Assistant.activate`. The screenshot
stays on local tmpfs and goes to the model only when the request refers to the
screen ("šta je ovo", "na ekranu") or when the model calls `look_at_screen`.
"""

import asyncio
import base64
import io
import logging
import re
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from claude_agent_sdk import tool
from PIL import Image, ImageDraw

log = logging.getLogger(__name__)

# Long edge Claude's vision works best at; larger images are downscaled server-side anyway.
MAX_EDGE = 1568
SELECTION_MAX_CHARS = 4000
SELECTION_FRESH_S = 90

DEICTIC = re.compile(
    r"\b(ovo|ovaj|ova|ove|ovog|ovoga|ovom|ovu|ovde|ovdje|ekran\w*|vidiš|vidis|pogledaj|"
    r"prikazan\w*|greška|grešku|error|this|here|screen)\b", re.I)


@dataclass
class ScreenContext:
    window_title: str = ""
    window_class: str = ""
    screenshot: str = ""
    selection: str = ""
    selection_age_s: float = 1e9
    captured_at: float = 0.0
    # "x,y,w,h,monitor_w,monitor_h": where the visible notch sits on the screenshot,
    # painted over instead of hiding the notch (no flicker on follow-ups).
    mask: str = ""

    @classmethod
    def from_dbus(cls, d: dict[str, str]) -> "ScreenContext":
        try:
            age = float(d.get("selection_age", "1e9"))
        except ValueError:
            age = 1e9
        return cls(window_title=d.get("window_title", ""), window_class=d.get("window_class", ""),
                   screenshot=d.get("screenshot", ""), selection=d.get("selection", ""),
                   selection_age_s=age, captured_at=time.time(), mask=d.get("mask", ""))

    def describe(self) -> str:
        """Text context prepended to Vuk's message."""
        parts = []
        if self.window_title or self.window_class:
            parts.append(f"aktivni prozor: {self.window_title} ({self.window_class})")
        if self.selection.strip() and self.selection_age_s < SELECTION_FRESH_S:
            text = self.selection.strip()[:SELECTION_MAX_CHARS]
            parts.append(f"nedavno selektovan tekst:\n<selekcija>\n{text}\n</selekcija>")
        return "\n".join(parts)


def jpeg_b64(path: str, mask: str = "") -> str | None:
    p = Path(path)
    if not path or not p.exists():
        return None
    with Image.open(p) as im:
        im = im.convert("RGB")
        if mask:
            x, y, w, h, mw, mh = (float(v) for v in mask.split(","))
            sx, sy = im.width / mw, im.height / mh
            ImageDraw.Draw(im).rectangle(
                [x * sx, y * sy, (x + w) * sx, (y + h) * sy], fill=(24, 24, 28))
        im.thumbnail((MAX_EDGE, MAX_EDGE))
        buf = io.BytesIO()
        im.save(buf, "JPEG", quality=80)
    return base64.b64encode(buf.getvalue()).decode()


def image_block(b64: str) -> dict[str, Any]:
    return {"type": "image", "source": {"type": "base64", "media_type": "image/jpeg", "data": b64}}


def make_screen_tool(get_context: Callable[[], ScreenContext],
                     fresh_context: Callable[[], Awaitable[ScreenContext]]):
    @tool("look_at_screen",
          "Vraća snimak Vukovog ekrana (koji monitor, zavisi od podešavanja). Podrazumevano snimak iz "
          "trenutka Vukovog poslednjeg zahteva; fresh=true pravi novi snimak sada.",
          {"fresh": bool})
    async def look_at_screen(args: dict[str, Any]) -> dict[str, Any]:
        ctx = await fresh_context() if args.get("fresh") else get_context()
        b64 = await asyncio.to_thread(jpeg_b64, ctx.screenshot, ctx.mask)
        if not b64:
            return {"content": [{"type": "text", "text": "Snimak ekrana nije dostupan."}],
                    "is_error": True}
        return {"content": [{"type": "image", "data": b64, "mimeType": "image/jpeg"}]}

    return look_at_screen
