"""Image loading, fonts, and the small drawing primitives the renderers share."""

from __future__ import annotations

from pathlib import Path
from typing import Optional, Sequence, Tuple

import numpy as np
from PIL import Image, ImageDraw, ImageFont

RGB = Tuple[int, int, int]

INK: RGB = (17, 19, 24)
MUTED: RGB = (110, 117, 130)
PAPER: RGB = (247, 248, 250)
LINE: RGB = (222, 226, 232)
CHANGE: RGB = (222, 41, 92)  # magenta-red, reads on both light and dark UI
MOVED: RGB = (214, 132, 16)  # amber: same content, new position
BEFORE_TINT: RGB = (214, 45, 60)
AFTER_TINT: RGB = (24, 122, 235)

_FONT_CANDIDATES = (
    "/System/Library/Fonts/SFNSDisplay.ttf",
    "/System/Library/Fonts/Helvetica.ttc",
    "/System/Library/Fonts/Supplemental/Arial.ttf",
    "/Library/Fonts/Arial.ttf",
    "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
)
_BOLD_CANDIDATES = (
    "/System/Library/Fonts/SFNSDisplay-Bold.otf",
    "/System/Library/Fonts/Supplemental/Arial Bold.ttf",
    "/Library/Fonts/Arial Bold.ttf",
    "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
)
_MONO_CANDIDATES = (
    "/System/Library/Fonts/Menlo.ttc",
    "/System/Library/Fonts/Monaco.ttf",
    "/usr/share/fonts/truetype/dejavu/DejaVuSansMono.ttf",
)


def load_font(size: int = 14, bold: bool = False, mono: bool = False) -> ImageFont.ImageFont:
    candidates = _MONO_CANDIDATES if mono else (_BOLD_CANDIDATES if bold else _FONT_CANDIDATES)
    for path in candidates:
        if Path(path).exists():
            try:
                return ImageFont.truetype(path, size)
            except OSError:
                continue
    # Bundled bitmap font: correct, just less pretty.
    return ImageFont.load_default()


def load_rgb(path: Path) -> Image.Image:
    """Open an image as RGB, flattening any alpha onto white."""
    img = Image.open(path)
    if img.mode in ("RGBA", "LA", "P"):
        img = img.convert("RGBA")
        canvas = Image.new("RGBA", img.size, (255, 255, 255, 255))
        canvas.alpha_composite(img)
        return canvas.convert("RGB")
    return img.convert("RGB")


def to_array(img: Image.Image) -> np.ndarray:
    return np.asarray(img, dtype=np.int16)


def fit_to(img: Image.Image, size: Tuple[int, int]) -> Image.Image:
    """Place an image on a white canvas of `size`, top-left anchored.

    Top-left rather than centred: phone screenshots that differ in height
    differ at the bottom (more content), and anchoring keeps the chrome and
    header aligned so the diff stays meaningful.
    """
    if img.size == size:
        return img
    canvas = Image.new("RGB", size, (255, 255, 255))
    canvas.paste(img, (0, 0))
    return canvas


def text_size(draw: ImageDraw.ImageDraw, text: str, font) -> Tuple[int, int]:
    try:
        box = draw.textbbox((0, 0), text, font=font)
        return (box[2] - box[0], box[3] - box[1])
    except AttributeError:  # very old Pillow
        return draw.textsize(text, font=font)


def draw_caption(
    draw: ImageDraw.ImageDraw,
    xy: Tuple[int, int],
    text: str,
    font,
    fill: RGB = INK,
) -> None:
    draw.text(xy, text, font=font, fill=fill)


def draw_badge(
    img: Image.Image,
    xy: Tuple[int, int],
    text: str,
    font,
    bg: RGB = CHANGE,
    fg: RGB = (255, 255, 255),
    pad: int = 6,
    radius: int = 6,
) -> Tuple[int, int]:
    """Draw a rounded pill label; returns its (w, h)."""
    draw = ImageDraw.Draw(img)
    tw, th = text_size(draw, text, font)
    w, h = tw + pad * 2, th + pad * 2
    box = [xy[0], xy[1], xy[0] + w, xy[1] + h]
    try:
        draw.rounded_rectangle(box, radius=radius, fill=bg)
    except AttributeError:
        draw.rectangle(box, fill=bg)
    draw.text((xy[0] + pad, xy[1] + pad - 1), text, font=font, fill=fg)
    return (w, h)


def dim(img: Image.Image, amount: float = 0.72) -> Image.Image:
    """Wash an image out toward white so highlights pop against it."""
    arr = np.asarray(img, dtype=np.float32)
    washed = arr + (255.0 - arr) * float(amount)
    return Image.fromarray(np.clip(washed, 0, 255).astype(np.uint8))


def tint_pixels(img: Image.Image, mask: np.ndarray, color: RGB, strength: float = 0.55) -> Image.Image:
    """Blend `color` into the pixels selected by a boolean mask."""
    arr = np.asarray(img, dtype=np.float32).copy()
    if not mask.any():
        return Image.fromarray(arr.astype(np.uint8))
    sel = mask.astype(bool)
    tint = np.array(color, dtype=np.float32)
    arr[sel] = arr[sel] * (1.0 - strength) + tint * strength
    return Image.fromarray(np.clip(arr, 0, 255).astype(np.uint8))


def stack_h(images: Sequence[Image.Image], gap: int = 24, bg: RGB = PAPER) -> Image.Image:
    width = sum(i.width for i in images) + gap * (len(images) - 1)
    height = max(i.height for i in images)
    canvas = Image.new("RGB", (width, height), bg)
    x = 0
    for img in images:
        canvas.paste(img, (x, 0))
        x += img.width + gap
    return canvas


def with_header(
    img: Image.Image,
    title: str,
    subtitle: str = "",
    accent: Optional[RGB] = None,
    pad: int = 16,
) -> Image.Image:
    """Put a titled card around a frame so a bare PNG explains itself."""
    title_font = load_font(17, bold=True)
    sub_font = load_font(13)
    head = 34 if not subtitle else 56
    canvas = Image.new("RGB", (img.width + pad * 2, img.height + head + pad * 2), (255, 255, 255))
    draw = ImageDraw.Draw(canvas)
    draw.rectangle([0, 0, canvas.width, head + pad // 2], fill=PAPER)
    if accent:
        draw.rectangle([0, 0, 4, head + pad // 2], fill=accent)
    draw.text((pad, pad - 4), title, font=title_font, fill=INK)
    if subtitle:
        draw.text((pad, pad + 18), subtitle, font=sub_font, fill=MUTED)
    canvas.paste(img, (pad, head + pad // 2 + pad // 2))
    draw.rectangle(
        [pad - 1, head + pad - 1, pad + img.width, head + pad + img.height],
        outline=LINE,
    )
    return canvas
