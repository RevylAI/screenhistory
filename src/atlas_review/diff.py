"""Visual diffing: change detection, change regions, and the three render modes.

The detector is deliberately not a raw pixel compare. Atlas frames are JPEGs
captured from a live device, so they carry compression speckle, and the iOS
status bar clock differs on every capture. A naive diff flags 100% of frames as
changed and the feature dies on arrival. So: per-pixel tolerance, then a block
grid with a density floor to kill speckle, then connected components to turn
scattered pixels into a handful of regions a human can actually look at.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence, Tuple

import numpy as np
from PIL import Image, ImageDraw

from . import imaging
from .imaging import AFTER_TINT, BEFORE_TINT, CHANGE, INK, MOVED, MUTED, PAPER
from .models import ScreenVersion

# Normalized (x, y, w, h) boxes, as fractions of width/height.
Box = Tuple[float, float, float, float]

IGNORE_PRESETS: Dict[str, Box] = {
    # Clock, carrier, wifi and battery all change between captures.
    "status_bar": (0.0, 0.0, 1.0, 0.07),
    "ios_home_indicator": (0.0, 0.985, 1.0, 0.015),
    "android_nav_bar": (0.0, 0.95, 1.0, 0.05),
}


@dataclass
class DiffOptions:
    """Tuning for change detection. Defaults are calibrated for phone frames."""

    tolerance: int = 32              # max per-channel delta treated as noise
    block: int = 8                   # block grid size in px
    min_block_density: float = 0.06  # fraction of a block's px that must change
    min_region_px: int = 220         # drop regions smaller than this (px area)
    merge_radius: int = 2            # blocks; glues adjacent glyphs into one region
    detect_shift: bool = True        # label regions that are just translated content
    ignore: Sequence[str] = field(default_factory=lambda: ["status_bar"])
    ignore_boxes: Sequence[Box] = field(default_factory=tuple)

    def masks(self) -> List[Box]:
        boxes: List[Box] = []
        for name in self.ignore:
            if name in IGNORE_PRESETS:
                boxes.append(IGNORE_PRESETS[name])
        boxes.extend(self.ignore_boxes)
        return boxes


@dataclass
class Region:
    """A contiguous area that changed, in pixel coordinates."""

    x: int
    y: int
    w: int
    h: int
    changed_pixels: int = 0
    image_size: Tuple[int, int] = (0, 0)
    kind: str = "changed"   # "changed" | "moved"
    shift_dy: int = 0       # vertical translation, when kind == "moved"

    @property
    def area(self) -> int:
        return self.w * self.h

    @property
    def description(self) -> str:
        """Human phrasing for a region, used in reports and alerts."""
        if self.kind == "moved" and self.shift_dy:
            arrow = "down" if self.shift_dy > 0 else "up"
            return "moved %s %dpx (%s)" % (arrow, abs(self.shift_dy), self.position_label)
        return "changed (%s)" % self.position_label

    @property
    def box(self) -> Tuple[int, int, int, int]:
        return (self.x, self.y, self.x + self.w, self.y + self.h)

    @property
    def normalized(self) -> Box:
        iw, ih = self.image_size
        if not iw or not ih:
            return (0.0, 0.0, 0.0, 0.0)
        return (self.x / iw, self.y / ih, self.w / iw, self.h / ih)

    @property
    def position_label(self) -> str:
        """Rough vertical placement, so alert text can say where to look."""
        _, ih = self.image_size
        if not ih:
            return "screen"
        center = (self.y + self.h / 2) / ih
        if center < 0.12:
            return "status/nav area"
        if center < 0.3:
            return "header"
        if center < 0.7:
            return "mid-screen"
        return "lower screen"

    def to_dict(self) -> Dict[str, Any]:
        return {
            "x": self.x,
            "y": self.y,
            "w": self.w,
            "h": self.h,
            "normalized": list(self.normalized),
            "changed_pixels": self.changed_pixels,
            "position": self.position_label,
            "kind": self.kind,
            "shift_dy": self.shift_dy,
            "description": self.description,
        }

    def overlaps(self, other: Box) -> bool:
        """Does this region intersect a normalized watch box?"""
        ox, oy, ow, oh = other
        iw, ih = self.image_size
        if not iw or not ih:
            return False
        a = (self.x, self.y, self.x + self.w, self.y + self.h)
        b = (ox * iw, oy * ih, (ox + ow) * iw, (oy + oh) * ih)
        return not (a[2] <= b[0] or b[2] <= a[0] or a[3] <= b[1] or b[3] <= a[1])


@dataclass
class DiffResult:
    """The comparison of one screen between two builds."""

    before: ScreenVersion
    after: ScreenVersion
    score: float                     # fraction of comparable pixels that changed
    changed_pixels: int
    comparable_pixels: int
    regions: List[Region]
    size_changed: bool = False
    options: DiffOptions = field(default_factory=DiffOptions)
    _mask: Optional[np.ndarray] = None
    _before_img: Optional[Image.Image] = None
    _after_img: Optional[Image.Image] = None

    @property
    def percent(self) -> float:
        return self.score * 100.0

    @property
    def changed(self) -> bool:
        return bool(self.regions) or self.size_changed

    @property
    def summary(self) -> str:
        if not self.changed:
            return "no visual change"
        bits = ["%.2f%% of pixels" % self.percent]
        moved = sum(1 for r in self.regions if r.kind == "moved")
        edited = len(self.regions) - moved
        if edited:
            bits.append("%d changed region%s" % (edited, "" if edited == 1 else "s"))
        if moved:
            bits.append("%d moved" % moved)
        if self.size_changed:
            bits.append("frame size changed")
        return ", ".join(bits)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "screen": self.after.screen.to_dict(),
            "before_build": self.before.build.to_dict(),
            "after_build": self.after.build.to_dict(),
            "score": self.score,
            "percent": self.percent,
            "changed": self.changed,
            "changed_pixels": self.changed_pixels,
            "size_changed": self.size_changed,
            "regions": [r.to_dict() for r in self.regions],
            "summary": self.summary,
        }

    # -- region queries ---------------------------------------------------

    def changed_in(self, box: Box) -> int:
        """Changed pixels strictly inside a normalized box.

        Summing the `changed_pixels` of every region that merely *touches* the
        box overcounts badly -- one tall region clipping the corner would
        contribute all of its pixels. This clips against the mask instead.
        """
        if self._mask is None:
            return 0
        h, w = self._mask.shape
        x0 = max(0, min(w, int(box[0] * w)))
        y0 = max(0, min(h, int(box[1] * h)))
        x1 = max(0, min(w, int((box[0] + box[2]) * w)))
        y1 = max(0, min(h, int((box[1] + box[3]) * h)))
        if x1 <= x0 or y1 <= y0:
            return 0
        return int(self._mask[y0:y1, x0:x1].sum())

    def changed_fraction_in(self, box: Box) -> float:
        """Changed pixels inside a box, as a fraction of that box's area."""
        if self._mask is None:
            return 0.0
        h, w = self._mask.shape
        area = max(1, int(box[2] * w) * int(box[3] * h))
        return self.changed_in(box) / area

    def regions_in(self, box: Box) -> List[Region]:
        return [r for r in self.regions if r.overlaps(box)]

    # -- renderers --------------------------------------------------------

    def render(self, mode: str = "highlight", scale: float = 1.0) -> Image.Image:
        modes = {
            "highlight": self.render_highlight,
            "side-by-side": self.render_side_by_side,
            "side_by_side": self.render_side_by_side,
            "overlay": self.render_overlay,
            "mask": self.render_mask,
        }
        if mode not in modes:
            raise ValueError("unknown diff mode %r (have: %s)" % (mode, ", ".join(sorted(modes))))
        img = modes[mode]()
        if scale != 1.0:
            img = img.resize((int(img.width * scale), int(img.height * scale)), Image.LANCZOS)
        return img

    def render_highlight(self, dim_amount: float = 0.78) -> Image.Image:
        """The after frame, washed out except where it changed, boxed in red."""
        base = self._after_image()
        if self._mask is None or not self._mask.any():
            return base.copy()
        washed = imaging.dim(base, dim_amount)
        # Punch the changed areas back to full strength, then tint + box them.
        arr = np.asarray(washed).copy()
        full = np.asarray(base)
        band = _dilate(self._mask, 2)
        arr[band] = full[band]
        out = Image.fromarray(arr)
        # Tint per region kind so amber "moved" content reads apart from real edits.
        moved_mask = np.zeros_like(self._mask)
        for region in self.regions:
            if region.kind == "moved":
                moved_mask[region.y:region.y + region.h, region.x:region.x + region.w] = True
        moved_mask &= self._mask
        out = imaging.tint_pixels(out, self._mask & ~moved_mask, CHANGE, strength=0.35)
        out = imaging.tint_pixels(out, moved_mask, MOVED, strength=0.30)
        draw = ImageDraw.Draw(out)
        font = imaging.load_font(12, bold=True)
        for i, region in enumerate(self.regions, start=1):
            pad = 3
            box = [
                max(0, region.x - pad),
                max(0, region.y - pad),
                min(out.width - 1, region.x + region.w + pad),
                min(out.height - 1, region.y + region.h + pad),
            ]
            color = MOVED if region.kind == "moved" else CHANGE
            draw.rectangle(box, outline=color, width=2)
            ly = box[1] - 17 if box[1] > 18 else box[3] + 2
            imaging.draw_badge(out, (box[0], ly), str(i), font, bg=color, pad=4, radius=4)
        legend = self._legend(self._kind_legend(), width=out.width)
        canvas = Image.new("RGB", (out.width, out.height + legend.height), (255, 255, 255))
        canvas.paste(out, (0, 0))
        canvas.paste(legend, (0, out.height))
        return canvas

    def render_side_by_side(self, gap: int = 28, boxes: bool = True) -> Image.Image:
        """Before and after as labelled cards, with change boxes on both."""
        before = self._before_image().copy()
        after = self._after_image().copy()
        if boxes and self.regions:
            for img in (before, after):
                draw = ImageDraw.Draw(img)
                for region in self.regions:
                    draw.rectangle(
                        region.box,
                        outline=MOVED if region.kind == "moved" else CHANGE,
                        width=2,
                    )
        left = imaging.with_header(
            before,
            self.before.build.label,
            self._provenance(self.before),
            accent=BEFORE_TINT,
        )
        right = imaging.with_header(
            after,
            self.after.build.label,
            self._provenance(self.after),
            accent=AFTER_TINT,
        )
        body = imaging.stack_h([left, right], gap=gap)
        return self._with_footer(body)

    def render_overlay(self, opacity: float = 0.5) -> Image.Image:
        """Onion-skin: unchanged pixels blend away, moved content ghosts.

        Content only in the before frame goes red, only in the after goes blue,
        so a shifted element reads as a red ghost next to a blue one.
        """
        before = np.asarray(self._before_image(), dtype=np.float32)
        after = np.asarray(self._after_image(), dtype=np.float32)
        blend = before * (1.0 - opacity) + after * opacity
        out = np.clip(blend, 0, 255)
        if self._mask is not None and self._mask.any():
            sel = self._mask
            # Darker pixel = more ink; whichever frame is darker "owns" that px.
            before_lum = before.mean(axis=2)
            after_lum = after.mean(axis=2)
            before_owns = sel & (before_lum < after_lum - 8)
            after_owns = sel & (after_lum < before_lum - 8)
            for owns, color in ((before_owns, BEFORE_TINT), (after_owns, AFTER_TINT)):
                if owns.any():
                    tint = np.array(color, dtype=np.float32)
                    out[owns] = out[owns] * 0.35 + tint * 0.65
        img = Image.fromarray(out.astype(np.uint8))
        legend = self._legend(
            [(BEFORE_TINT, "only in %s" % self.before.build.label),
             (AFTER_TINT, "only in %s" % self.after.build.label)],
            width=img.width,
        )
        canvas = Image.new("RGB", (img.width, img.height + legend.height), (255, 255, 255))
        canvas.paste(img, (0, 0))
        canvas.paste(legend, (0, img.height))
        return canvas

    def render_mask(self) -> Image.Image:
        """Raw change mask -- mostly for tuning thresholds."""
        h, w = (self._after_image().height, self._after_image().width)
        arr = np.full((h, w, 3), 255, dtype=np.uint8)
        if self._mask is not None:
            arr[self._mask] = np.array(CHANGE, dtype=np.uint8)
        return Image.fromarray(arr)

    # -- helpers ----------------------------------------------------------

    def _before_image(self) -> Image.Image:
        if self._before_img is None:
            raise RuntimeError("diff result has no cached images")
        return self._before_img

    def _after_image(self) -> Image.Image:
        if self._after_img is None:
            raise RuntimeError("diff result has no cached images")
        return self._after_img

    @staticmethod
    def _provenance(version: ScreenVersion) -> str:
        build = version.build
        bits = []
        if build.branch:
            bits.append(build.branch)
        if build.subject:
            subject = build.subject
            bits.append(subject if len(subject) <= 40 else subject[:39] + "…")
        bits.append(build.relative_time)
        return " · ".join(bits)

    def _kind_legend(self) -> List[Tuple[Tuple[int, int, int], str]]:
        moved = sum(1 for r in self.regions if r.kind == "moved")
        entries: List[Tuple[Tuple[int, int, int], str]] = []
        if len(self.regions) - moved:
            entries.append((CHANGE, "changed content"))
        if moved:
            entries.append((MOVED, "same content, moved"))
        return entries

    def _legend(self, entries: Sequence[Tuple[Tuple[int, int, int], str]], width: int) -> Image.Image:
        font = imaging.load_font(13)
        img = Image.new("RGB", (width, 34), PAPER)
        draw = ImageDraw.Draw(img)
        x = 12
        for color, text in entries:
            draw.rectangle([x, 12, x + 11, 23], fill=color)
            draw.text((x + 18, 11), text, font=font, fill=INK)
            x += 30 + imaging.text_size(draw, text, font)[0]
        return img

    def _with_footer(self, body: Image.Image) -> Image.Image:
        font = imaging.load_font(13)
        bold = imaging.load_font(13, bold=True)
        legend = self._legend(self._kind_legend(), width=body.width)
        img = Image.new("RGB", (body.width, body.height + 40 + legend.height), PAPER)
        img.paste(body, (0, 0))
        draw = ImageDraw.Draw(img)
        head = "%s · %s" % (self.after.screen.name, self.summary)
        draw.text((14, body.height + 12), head, font=bold, fill=INK)
        used = 14 + imaging.text_size(draw, head, bold)[0] + 16
        tail = _fit_text(
            draw, "%s -> %s" % (self.before.build.title, self.after.build.title),
            font, max(40, body.width - used - 14),
        )
        draw.text((used, body.height + 12), tail, font=font, fill=MUTED)
        img.paste(legend, (0, body.height + 40))
        return img


def _fit_text(draw: ImageDraw.ImageDraw, text: str, font, max_width: int) -> str:
    """Trim a string with an ellipsis until it fits `max_width`."""
    if imaging.text_size(draw, text, font)[0] <= max_width:
        return text
    trimmed = text
    while trimmed and imaging.text_size(draw, trimmed + "...", font)[0] > max_width:
        trimmed = trimmed[:-1]
    return (trimmed + "...") if trimmed else ""


def _dilate(mask: np.ndarray, radius: int) -> np.ndarray:
    """Boolean dilation by shifting -- avoids a scipy dependency."""
    if radius <= 0:
        return mask
    out = mask.copy()
    for dy in range(-radius, radius + 1):
        for dx in range(-radius, radius + 1):
            if dx == 0 and dy == 0:
                continue
            out |= np.roll(np.roll(mask, dy, axis=0), dx, axis=1)
    return out


def _detect_shift(
    before_gray: np.ndarray,
    after_gray: np.ndarray,
    region: Region,
    max_shift: int = 96,
    min_gain: float = 0.45,
) -> int:
    """Is this region the same content translated vertically? Returns dy (0 = no).

    Inserting a row of content pushes everything below it down, which a pixel
    diff reports as "the entire list changed". Recognising the translation lets
    the report say "12 rows moved down 22px" instead, which is the difference
    between a reviewer trusting the tool and ignoring it.
    """
    h, w = after_gray.shape
    y0, y1 = region.y, min(h, region.y + region.h)
    x0, x1 = region.x, min(w, region.x + region.w)
    if y1 - y0 < 16 or x1 - x0 < 16:
        return 0
    patch = after_gray[y0:y1, x0:x1]
    baseline = float(np.abs(patch - before_gray[y0:y1, x0:x1]).mean())
    if baseline < 1.0:
        return 0
    best_dy, best_err = 0, baseline
    for dy in range(-max_shift, max_shift + 1, 2):
        if dy == 0:
            continue
        sy0, sy1 = y0 - dy, y1 - dy
        if sy0 < 0 or sy1 > h:
            continue
        err = float(np.abs(patch - before_gray[sy0:sy1, x0:x1]).mean())
        if err < best_err:
            best_dy, best_err = dy, err
    # A best match sitting on the edge of the search window usually means no
    # true alignment was found -- in a list of evenly spaced rows the search
    # happily locks onto a *different* row. Treat that as "not a move".
    if abs(best_dy) >= max_shift:
        return 0
    # Only call it a move if aligning genuinely explains most of the difference.
    if best_dy and best_err < baseline * min_gain and best_err < 12.0:
        return best_dy
    return 0


def _label_blocks(grid: np.ndarray, merge_radius: int) -> List[np.ndarray]:
    """Connected components over the block grid, 8-connected, iterative BFS."""
    search = _dilate(grid, merge_radius) if merge_radius > 0 else grid
    seen = np.zeros_like(search, dtype=bool)
    rows, cols = search.shape
    components: List[np.ndarray] = []
    for r0 in range(rows):
        for c0 in range(cols):
            if not search[r0, c0] or seen[r0, c0]:
                continue
            stack = [(r0, c0)]
            seen[r0, c0] = True
            cells: List[Tuple[int, int]] = []
            while stack:
                r, c = stack.pop()
                cells.append((r, c))
                for dr in (-1, 0, 1):
                    for dc in (-1, 0, 1):
                        rr, cc = r + dr, c + dc
                        if 0 <= rr < rows and 0 <= cc < cols and search[rr, cc] and not seen[rr, cc]:
                            seen[rr, cc] = True
                            stack.append((rr, cc))
            member = np.zeros_like(search, dtype=bool)
            for r, c in cells:
                member[r, c] = True
            # Report the extent of genuinely-changed blocks, not the dilation.
            real = member & grid
            if real.any():
                components.append(real)
    return components


ASPECT_TOLERANCE = 0.02


def normalize_pair(
    before_img: Image.Image,
    after_img: Image.Image,
    target: Optional[int] = None,
) -> Tuple[Image.Image, Image.Image, bool]:
    """Put two frames on a common scale. Returns (before, after, aspect_changed).

    The same screen can be captured at 1x and 3x on different devices -- Atlas
    hands back whatever the run produced. Padding a 1320px frame and a 589px
    frame to a common canvas compares a screenshot against mostly whitespace
    and reports ~95% changed, which is noise dressed as a finding. Scaling both
    to the smaller width compares like with like.

    A genuine *shape* change (rotation, a different device aspect) survives as
    the returned flag, which is the signal worth alerting on.
    """
    if before_img.size == after_img.size and not target:
        return before_img, after_img, False

    a_ratio = before_img.height / before_img.width
    b_ratio = after_img.height / after_img.width
    aspect_changed = abs(a_ratio - b_ratio) > ASPECT_TOLERANCE * max(a_ratio, b_ratio)

    target = target or min(before_img.width, after_img.width)
    if before_img.width != target:
        before_img = before_img.resize(
            (target, max(1, round(before_img.height * target / before_img.width))), Image.LANCZOS)
    if after_img.width != target:
        after_img = after_img.resize(
            (target, max(1, round(after_img.height * target / after_img.width))), Image.LANCZOS)
    return before_img, after_img, aspect_changed


def compare_images(
    before_img: Image.Image,
    after_img: Image.Image,
    options: Optional[DiffOptions] = None,
    target_width: Optional[int] = None,
) -> Tuple[np.ndarray, List[Region], int, int, bool]:
    """Core detector. Returns (mask, regions, changed_px, comparable_px, resized)."""
    options = options or DiffOptions()
    before_img, after_img, size_changed = normalize_pair(before_img, after_img, target_width)
    size = (max(before_img.width, after_img.width), max(before_img.height, after_img.height))
    before = imaging.fit_to(before_img, size)
    after = imaging.fit_to(after_img, size)

    a = np.asarray(before, dtype=np.int16)
    b = np.asarray(after, dtype=np.int16)
    delta = np.abs(a - b).max(axis=2)

    ignore = np.zeros(delta.shape, dtype=bool)
    w, h = size
    for bx, by, bw, bh in options.masks():
        x0, y0 = int(bx * w), int(by * h)
        x1, y1 = int((bx + bw) * w), int((by + bh) * h)
        ignore[max(0, y0):min(h, y1), max(0, x0):min(w, x1)] = True
    delta[ignore] = 0

    raw = delta > options.tolerance
    comparable = int((~ignore).sum())

    # Block grid: a block counts only if enough of its pixels moved.
    block = max(1, options.block)
    rows, cols = (h + block - 1) // block, (w + block - 1) // block
    padded = np.zeros((rows * block, cols * block), dtype=np.int32)
    padded[:h, :w] = raw.astype(np.int32)
    counts = padded.reshape(rows, block, cols, block).sum(axis=(1, 3))
    grid = counts >= max(1, int(options.min_block_density * block * block))

    mask = np.zeros((h, w), dtype=bool)
    regions: List[Region] = []
    if grid.any():
        expanded = np.repeat(np.repeat(grid, block, axis=0), block, axis=1)[:h, :w]
        mask = raw & expanded
        for member in _label_blocks(grid, options.merge_radius):
            rs, cs = np.nonzero(member)
            y0, y1 = int(rs.min() * block), int(min(h, (rs.max() + 1) * block))
            x0, x1 = int(cs.min() * block), int(min(w, (cs.max() + 1) * block))
            region_mask = np.zeros((h, w), dtype=bool)
            region_mask[y0:y1, x0:x1] = True
            changed_here = int((mask & region_mask).sum())
            region = Region(
                x=x0, y=y0, w=x1 - x0, h=y1 - y0,
                changed_pixels=changed_here,
                image_size=(w, h),
            )
            if region.area >= options.min_region_px:
                regions.append(region)
    if options.detect_shift and regions:
        before_gray = a.mean(axis=2).astype(np.float32)
        after_gray = b.mean(axis=2).astype(np.float32)
        for region in regions:
            dy = _detect_shift(before_gray, after_gray, region)
            if dy:
                region.kind = "moved"
                region.shift_dy = dy
    regions.sort(key=lambda r: r.changed_pixels, reverse=True)
    changed_px = int(mask.sum())
    return mask, regions, changed_px, comparable, size_changed


def diff_versions(
    before: ScreenVersion,
    after: ScreenVersion,
    options: Optional[DiffOptions] = None,
    target_width: Optional[int] = None,
) -> DiffResult:
    """Compare one screen between two builds.

    `target_width` pins the scale both frames are normalized to. Callers that
    know a screen's whole history (see `ScreenHistory.target_width`) pass the
    screen-wide value, so a pair's numbers do not depend on which pair it is.
    """
    options = options or DiffOptions()
    if not before.has_image or not after.has_image:
        missing = before.build.label if not before.has_image else after.build.label
        raise ValueError("no cached screenshot for %s at build %s" % (after.screen.name, missing))
    before_img = imaging.load_rgb(Path(before.image_path))
    after_img = imaging.load_rgb(Path(after.image_path))
    mask, regions, changed_px, comparable, resized = compare_images(
        before_img, after_img, options, target_width)
    # Renderers must draw the same normalized frames the detector measured.
    before_img, after_img, _ = normalize_pair(before_img, after_img, target_width)
    size = (max(before_img.width, after_img.width), max(before_img.height, after_img.height))
    result = DiffResult(
        before=before,
        after=after,
        score=(changed_px / comparable) if comparable else 0.0,
        changed_pixels=changed_px,
        comparable_pixels=comparable,
        regions=regions,
        size_changed=resized,
        options=options,
    )
    result._mask = mask
    result._before_img = imaging.fit_to(before_img, size)
    result._after_img = imaging.fit_to(after_img, size)
    return result
