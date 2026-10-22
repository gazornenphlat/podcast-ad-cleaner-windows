"""An "AD-FREE" badge on episode and show artwork, so cleaned episodes stand out in your podcast app.
Idea from Podcast Ad Remover (github.com/jdcb4/podcast-ad-remover, MIT licence)."""
from __future__ import annotations

import io
import logging

log = logging.getLogger(__name__)

TEXT = "AD-FREE"
FILL = (27, 94, 32)          # deep green
INK = (255, 255, 255)
FONTS = ("segoeuib.ttf", "arialbd.ttf", "DejaVuSans-Bold.ttf", "LiberationSans-Bold.ttf")


def _font(size: int):
    from PIL import ImageFont
    for name in FONTS:
        try:
            return ImageFont.truetype(name, size)
        except OSError:
            continue
    try:
        return ImageFont.load_default(size)
    except TypeError:                    # Pillow older than 10.1
        return ImageFont.load_default()


def badge(data: bytes, max_side: int = 1400) -> tuple[bytes, str] | None:
    """Returns (JPEG bytes, mime) with a pill-shaped badge in the bottom-right corner, or None."""
    try:
        from PIL import Image, ImageDraw
        img = Image.open(io.BytesIO(data))
        img = img.convert("RGB")
        if max(img.size) > max_side:
            img.thumbnail((max_side, max_side))
        w, h = img.size
        side = min(w, h)
        font = _font(max(10, int(side * 0.065)))
        draw = ImageDraw.Draw(img)
        l, t, r, b = draw.textbbox((0, 0), TEXT, font=font)
        tw, th = r - l, b - t
        pad_x, pad_y = int(th * 0.9), int(th * 0.55)
        bw, bh = tw + 2 * pad_x, th + 2 * pad_y
        margin = int(side * 0.035)
        x1, y1 = w - margin, h - margin
        x0, y0 = x1 - bw, y1 - bh
        shadow = max(1, side // 300)
        draw.rounded_rectangle((x0 + shadow, y0 + shadow, x1 + shadow, y1 + shadow), radius=bh // 2, fill=(0, 0, 0))
        draw.rounded_rectangle((x0, y0, x1, y1), radius=bh // 2, fill=FILL, outline=INK,
                               width=max(1, side // 250))
        draw.text((x0 + pad_x - l, y0 + pad_y - t), TEXT, font=font, fill=INK)
        out = io.BytesIO()
        img.save(out, "JPEG", quality=90)
        return out.getvalue(), "image/jpeg"
    except Exception as e:  # noqa: BLE001 - the plain artwork is fine too
        log.warning("Couldn't add the ad-free badge to the artwork: %s", e)
        return None
