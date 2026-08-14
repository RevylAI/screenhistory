"""Builds the standalone HTML review tool.

Python's job here is only to gather: pull each screen's frame per build, attach
provenance and review state, run the policy once, and inline it all into a
single file. Every comparison is computed in the browser from those frames.

That split is deliberate. Pre-rendering diffs server-side means the report can
only show the pairs someone guessed at ahead of time -- pick any other pair and
you get an apology. Shipping n frames instead of n^2 renders makes every pair
available in both directions, makes the thresholds adjustable, and makes the
file smaller.
"""

from __future__ import annotations

import base64
import datetime as _dt
import hashlib
import io
import json
import os
import shutil
import webbrowser
from pathlib import Path
from typing import TYPE_CHECKING, Any, Dict, List, Optional, Sequence

from .models import Screen

if TYPE_CHECKING:  # pragma: no cover
    from .api import AtlasReview

ASSETS = Path(__file__).parent / "assets"


def _viewer_name() -> str:
    return os.environ.get("ATLAS_REVIEW_USER") or os.environ.get("USER") or "reviewer"


_MIME = {".png": "image/png", ".jpg": "image/jpeg", ".jpeg": "image/jpeg", ".webp": "image/webp"}

# Past this many frames the single-file report stops being pleasant to open.
FRAME_WARN_THRESHOLD = 140


def _frame_width(image_path: Path) -> int:
    from PIL import Image

    with Image.open(image_path) as probe:
        return probe.size[0]


def _frame_payload(image_path: Path, target_width: int) -> Dict[str, Any]:
    """A screenshot as a data URI plus its dimensions, rendered at `target_width`.

    A frame already at the target is embedded byte-for-byte: re-encoding would
    inject compression noise into the exact pixels the browser is about to
    measure, and the numbers would quietly disagree with `atlas-review diff`.
    Anything else is resampled to the target, because a screen captured at 3x
    on one device and 1x on another has to be compared on one scale (see
    `diff.normalize_pair`). Resampled frames go out as PNG.
    """
    from PIL import Image

    from . import imaging

    with Image.open(image_path) as probe:
        width, height = probe.size
    if width == target_width:
        mime = _MIME.get(image_path.suffix.lower(), "image/png")
        encoded = base64.b64encode(image_path.read_bytes()).decode("ascii")
        return {"src": "data:%s;base64,%s" % (mime, encoded), "w": width, "h": height}

    img = imaging.load_rgb(image_path)
    scaled = img.resize(
        (target_width, max(1, round(img.height * target_width / img.width))), Image.LANCZOS)
    buffer = io.BytesIO()
    scaled.save(buffer, "PNG", optimize=True)
    encoded = base64.b64encode(buffer.getvalue()).decode("ascii")
    return {"src": "data:image/png;base64," + encoded, "w": scaled.width, "h": scaled.height}


def build_report(
    review: "AtlasReview",
    out: Path,
    screens: Optional[Sequence[Screen]] = None,
    inline: bool = True,
    open_browser: bool = False,
    max_width: int = 460,
    quality: int = 88,
    served: bool = False,
    builds_limit: Optional[int] = None,
    log: Optional[Any] = None,
) -> Path:
    """Write the report. `out` may be a directory or an .html file path."""
    out = Path(out)
    say = log if callable(log) else (lambda *_a, **_k: None)
    if out.suffix.lower() in (".html", ".htm"):
        index = out
        out.parent.mkdir(parents=True, exist_ok=True)
    else:
        out.mkdir(parents=True, exist_ok=True)
        index = out / "index.html"

    all_builds = review.builds()
    builds = all_builds
    if builds_limit and builds_limit < len(all_builds):
        builds = all_builds[-builds_limit:]
        say("limited to the last %d of %d builds (--builds)" % (len(builds), len(all_builds)))
    kept = {b.id for b in builds}

    targets = list(screens) if screens else review.screens()
    if screens:
        say("limited to %d of %d screens (--screen)" % (len(targets), len(review.screens())))
    alert_report = review.check()

    # One copy of each distinct image, referenced by hash. A screen that did
    # not change between builds otherwise ships the same bytes once per build,
    # which is most of the weight on a real app.
    images: Dict[str, str] = {}

    payload: Dict[str, Any] = {
        "app": review.app,
        "generated_at": _dt.datetime.now().strftime("%b %d, %Y %H:%M"),
        "served": bool(served),
        "user": _viewer_name(),
        "builds": [b.to_dict() for b in builds],
        "screens": [],
        "images": images,
        "alerts": [a.to_dict() for a in alert_report.sorted()],
        "defaults": {
            "tolerance": review.options.tolerance,
            "block": review.options.block,
            "minBlockDensity": review.options.min_block_density,
            "minRegionPx": review.options.min_region_px,
            "mergeRadius": review.options.merge_radius,
            "ignoreStatusBar": "status_bar" in list(review.options.ignore),
            "detectShift": review.options.detect_shift,
        },
    }

    total_frames = 0
    for screen in targets:
        versions = [v for v in review.history(screen).versions() if v.build.id in kept]
        if not versions:
            continue
        baseline = review.baseline(screen)
        # One width for every frame of a screen, matching what the detector
        # normalizes to, so the browser and the CLI measure the same pixels.
        target_width = min([_frame_width(Path(v.image_path)) for v in versions] + [max_width])
        frames: List[Dict[str, Any]] = []
        for version in versions:
            frame = _frame_payload(Path(version.image_path), target_width)
            digest = hashlib.sha1(frame["src"].encode("ascii")).hexdigest()[:16]
            if digest not in images:
                images[digest] = frame["src"]
            frame.pop("src")
            frame.update({
                "img": digest,
                "build_id": version.build.id,
                "screen_id": screen.id,
                "observation_count": version.observation_count,
                "status": review.store.status(screen.id, version.build.id),
                "comments": [c.to_dict() for c in review.store.comments_for(screen.id, version.build.id)],
            })
            frames.append(frame)
        total_frames += len(frames)
        payload["screens"].append({
            "id": screen.id,
            "name": screen.name,
            "product_area": screen.product_area,
            "screen_kind": screen.screen_kind,
            "description": screen.description,
            "viewer_url": screen.viewer_url(review.app),
            "baseline_build": baseline.id if baseline else None,
            "frames": frames,
        })

    payload["screens"].sort(key=lambda s: (s["product_area"] or "~", s["name"]))

    # A build nothing was ever observed on has no frames to compare. Leaving it
    # in the pickers just offers a comparison that can only say "not in this
    # build", so drop it -- but say so, rather than silently showing fewer.
    seen_builds = {f["build_id"] for s in payload["screens"] for f in s["frames"]}
    unmapped = [b for b in payload["builds"] if b["id"] not in seen_builds]
    if unmapped and seen_builds:
        payload["builds"] = [b for b in payload["builds"] if b["id"] in seen_builds]
        say("skipped %d build(s) with no Atlas data: %s"
            % (len(unmapped), ", ".join(b["label"] for b in unmapped)))

    if total_frames:
        say("%d screens · %d frames · %d unique images (%d deduplicated)"
            % (len(payload["screens"]), total_frames, len(images), total_frames - len(images)))
    if total_frames > FRAME_WARN_THRESHOLD:
        say("note: %d frames is a large single file; narrow it with "
            "--builds N or --screen <name> if it feels slow" % total_frames)

    css = (ASSETS / "report.css").read_text()
    js = (ASSETS / "report.js").read_text()
    html = _SHELL
    html = html.replace("/*__CSS__*/", css)
    # Split the closing script tag so a literal one inside the data can't end it.
    html = html.replace("__DATA__", json.dumps(payload).replace("</", "<\\/"))
    html = html.replace("/*__JS__*/", js)
    index.write_text(html)

    if not inline:
        # Assets alongside the HTML are handy when iterating on the tool itself.
        (index.parent / "report.css").write_text(css)
        (index.parent / "report.js").write_text(js)
    if open_browser:
        webbrowser.open(index.resolve().as_uri())
    return index


def serve_dir(review: "AtlasReview", out: Path) -> Path:
    """Report prepared for the local review server."""
    return build_report(review, out=out, served=True)


_SHELL = """<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Atlas visual review</title>
<style>
/*__CSS__*/
</style>
</head>
<body>
<div class="app">
  <aside class="rail">
    <div class="brand">
      <h1><button type="button" class="home" id="home">Atlas visual review</button></h1>
      <div class="app-id" id="app-id"></div>
    </div>
    <div class="railtools">
      <input type="search" id="filter" placeholder="Filter screens" aria-label="Filter screens">
      <select id="sort" aria-label="Sort screens">
        <option value="change">Most changed</option>
        <option value="name">By area / name</option>
      </select>
    </div>
    <h2><span id="screen-count">Screens</span></h2>
    <ul class="screens" id="screens"></ul>
    <h2>Policy alerts</h2>
    <ul class="alerts" id="alerts"></ul>
  </aside>

  <main class="stage">
    <nav class="crumb" id="crumb"></nav>
    <div class="bar">
      <div class="pickers">
        <select id="sel-before" aria-label="Before build"></select>
        <button class="ghost" id="swap" title="Swap (s)">&#8646;</button>
        <select id="sel-after" aria-label="After build"></select>
      </div>
      <span class="seg" id="modes">
        <button data-mode="highlight" aria-pressed="true">highlight</button>
        <button data-mode="overlay" aria-pressed="false">overlay</button>
        <button data-mode="side-by-side" aria-pressed="false">side-by-side</button>
        <button data-mode="swipe" aria-pressed="false">swipe</button>
      </span>
      <span class="spacer"></span>
      <button class="ghost" id="export-png">Export PNG</button>
    </div>
    <div class="canvas-wrap" id="stage-body"></div>
    <div class="readout" id="readout"></div>
    <div class="film-wrap" id="film-wrap">
      <div class="film-head">
        <h2>Build history</h2>
        <span class="hint">click sets <b>after</b> &middot; shift-click sets <b>before</b></span>
      </div>
      <div class="film" id="film"></div>
    </div>
  </main>

  <aside class="side" id="side"></aside>

  <footer>
    <span id="meta"></span>
    <span class="spacer"></span>
    <span><kbd>&larr;</kbd><kbd>&rarr;</kbd> build</span>
    <span><kbd>1</kbd>&ndash;<kbd>4</kbd> mode</span>
    <span><kbd>s</kbd> swap</span>
    <span id="mode-note"></span>
    <button class="ghost" id="export-review">Download review.json</button>
  </footer>
</div>
<script>window.__ATLAS_DATA__ = __DATA__;</script>
<script>
/*__JS__*/
</script>
</body>
</html>
"""
