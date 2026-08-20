/* atlas-review report.
 *
 * The report ships raw frames, not pre-rendered diffs, and computes every
 * comparison here. That is what makes it a tool rather than a slideshow: any
 * build pair in either direction, thresholds you can drag, and a file that
 * grows with the number of screenshots instead of their square.
 */
(function () {
"use strict";

var DATA = window.__ATLAS_DATA__;
var LSKEY = "atlas-review:" + DATA.app;

/* ---------------------------------------------------------------- state */

var local = { decisions: [], comments: [] };
try { local = JSON.parse(localStorage.getItem(LSKEY)) || local; } catch (e) {}

var opts = Object.assign({
  tolerance: 32, block: 8, minBlockDensity: 0.06, minRegionPx: 220,
  mergeRadius: 2, ignoreStatusBar: true, detectShift: true, ignoreBoxes: []
}, DATA.defaults || {});

/* `before`/`after` are app-wide build ids, not per-screen. That is what makes
   the overview possible: one pair of builds, every screen measured against it,
   and drilling into a screen keeps the comparison you were already looking at. */
var ui = { view: "overview", screen: null, before: null, after: null,
           mode: "highlight", swipe: 0.5, filter: "", sort: "change" };
var overviewPair = "";  // the build pair `overview` was measured against
var deltas = {};        // screenId -> { buildId: percent } for adjacent pairs
var firstChange = {};   // screenId -> buildId
var overview = {};      // screenId -> {percent, regions, moved, missing} for the current pair
var overviewToken = 0;

/* ------------------------------------------------------------- plumbing */

function esc(s) {
  return String(s == null ? "" : s).replace(/[&<>"]/g, function (c) {
    return { "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;" }[c];
  });
}
function el(id) { return document.getElementById(id); }
function screenById(id) {
  for (var i = 0; i < DATA.screens.length; i++) if (DATA.screens[i].id === id) return DATA.screens[i];
  return null;
}
function buildById(id) {
  for (var i = 0; i < DATA.builds.length; i++) if (DATA.builds[i].id === id) return DATA.builds[i];
  return null;
}
function frameFor(screen, buildId) {
  for (var i = 0; i < screen.frames.length; i++) {
    if (screen.frames[i].build_id === buildId) return screen.frames[i];
  }
  return null;
}
/* Frames reference images by hash so an unchanged screen ships its bytes once. */
function srcOf(frame) { return DATA.images[frame.img]; }
function statusOf(sid, bid) {
  var hits = local.decisions.filter(function (d) { return d.screen_id === sid && d.build_id === bid; });
  if (hits.length) return hits[hits.length - 1].status;
  var s = screenById(sid), f = s && frameFor(s, bid);
  return f ? f.status : "pending";
}
function commentsOf(sid, bid) {
  var s = screenById(sid), f = s && frameFor(s, bid);
  var base = f ? f.comments.slice() : [];
  return base.concat(local.comments.filter(function (c) {
    return c.screen_id === sid && c.build_id === bid;
  }));
}

/* --------------------------------------------------------- image access */

var pixelCache = new Map();

function loadImage(src) {
  return new Promise(function (resolve, reject) {
    var img = new Image();
    img.onload = function () { resolve(img); };
    img.onerror = function () { reject(new Error("frame failed to decode")); };
    img.src = src;
  });
}

/* Frame pixels padded onto a white W*H canvas, top-left anchored. Anchoring
   beats centring: frames that differ in height differ at the bottom, so the
   header and chrome stay aligned and the diff stays meaningful. */
function pixels(frame, W, H) {
  var key = frame.build_id + "|" + frame.screen_id + "|" + W + "x" + H;
  if (pixelCache.has(key)) return Promise.resolve(pixelCache.get(key));
  return loadImage(srcOf(frame)).then(function (img) {
    var c = document.createElement("canvas");
    c.width = W; c.height = H;
    var ctx = c.getContext("2d", { willReadFrequently: true });
    ctx.fillStyle = "#fff"; ctx.fillRect(0, 0, W, H);
    ctx.drawImage(img, 0, 0);
    var data = ctx.getImageData(0, 0, W, H);
    pixelCache.set(key, data);
    return data;
  });
}

/* ---------------------------------------------------------- diff engine */

function dilate(mask, w, h, r) {
  if (r <= 0) return mask;
  var out = new Uint8Array(mask.length);
  for (var y = 0; y < h; y++) {
    for (var x = 0; x < w; x++) {
      if (!mask[y * w + x]) continue;
      var y0 = Math.max(0, y - r), y1 = Math.min(h - 1, y + r);
      var x0 = Math.max(0, x - r), x1 = Math.min(w - 1, x + r);
      for (var yy = y0; yy <= y1; yy++) {
        for (var xx = x0; xx <= x1; xx++) out[yy * w + xx] = 1;
      }
    }
  }
  return out;
}

function grayscale(data, w, h) {
  var g = new Float32Array(w * h), p = data.data;
  for (var i = 0, j = 0; i < g.length; i++, j += 4) g[i] = (p[j] + p[j + 1] + p[j + 2]) / 3;
  return g;
}

/* Same shape as the Python detector: tolerance, then a block grid with a
   density floor to kill JPEG speckle, then connected components. */
function ignoreMask(W, H, o) {
  // Fractional (x, y, w, h) boxes, floored -- not rounded -- because the
  // Python detector slices with int(), and the two must agree to the digit or
  // a CI gate and this page will contradict each other. The checkbox owns the
  // status bar; the rest ride in the payload from the policy.
  var boxes = o.ignoreStatusBar ? [[0, 0, 1, 0.07]] : [];
  boxes = boxes.concat(o.ignoreBoxes || []);
  if (!boxes.length) return null;
  var m = new Uint8Array(W * H), count = 0;
  for (var k = 0; k < boxes.length; k++) {
    var box = boxes[k];
    var x0 = Math.max(0, Math.floor(box[0] * W)), y0 = Math.max(0, Math.floor(box[1] * H));
    var x1 = Math.min(W, Math.floor((box[0] + box[2]) * W));
    var y1 = Math.min(H, Math.floor((box[1] + box[3]) * H));
    for (var y = y0; y < y1; y++) {
      for (var x = x0; x < x1; x++) {
        var i = y * W + x;
        if (!m[i]) { m[i] = 1; count++; }
      }
    }
  }
  return { mask: m, count: count };
}

function computeDiff(A, B, W, H, o) {
  var a = A.data, b = B.data, n = W * H;
  var raw = new Uint8Array(n);
  var ig = ignoreMask(W, H, o);
  var comparable = n - (ig ? ig.count : 0);

  for (var i = 0, j = 0; i < n; i++, j += 4) {
    if (ig && ig.mask[i]) continue;
    var d = Math.abs(a[j] - b[j]);
    var d2 = Math.abs(a[j + 1] - b[j + 1]); if (d2 > d) d = d2;
    var d3 = Math.abs(a[j + 2] - b[j + 2]); if (d3 > d) d = d3;
    if (d > o.tolerance) raw[i] = 1;
  }

  var bs = Math.max(1, o.block);
  var cols = Math.ceil(W / bs), rows = Math.ceil(H / bs);
  var counts = new Int32Array(cols * rows);
  for (var y = 0; y < H; y++) {
    var br = (y / bs) | 0;
    for (var x = 0; x < W; x++) {
      if (raw[y * W + x]) counts[br * cols + ((x / bs) | 0)]++;
    }
  }
  var floor = Math.max(1, Math.floor(o.minBlockDensity * bs * bs));
  var grid = new Uint8Array(cols * rows);
  for (var c = 0; c < grid.length; c++) if (counts[c] >= floor) grid[c] = 1;

  var mask = new Uint8Array(n), changed = 0;
  for (var y2 = 0; y2 < H; y2++) {
    var br2 = (y2 / bs) | 0;
    for (var x2 = 0; x2 < W; x2++) {
      var idx = y2 * W + x2;
      if (raw[idx] && grid[br2 * cols + ((x2 / bs) | 0)]) { mask[idx] = 1; changed++; }
    }
  }

  var regions = label(grid, cols, rows, bs, W, H, mask, o);
  if (o.detectShift && regions.length) {
    var ga = grayscale(A, W, H), gb = grayscale(B, W, H);
    for (var r = 0; r < regions.length; r++) annotateShift(regions[r], ga, gb, W, H);
  }
  regions.sort(function (p, q) { return q.changedPixels - p.changedPixels; });
  return { w: W, h: H, mask: mask, regions: regions, changed: changed, comparable: comparable };
}

function label(grid, cols, rows, bs, W, H, mask, o) {
  var search = dilate(grid, cols, rows, o.mergeRadius);
  var seen = new Uint8Array(cols * rows), out = [];
  for (var r0 = 0; r0 < rows; r0++) {
    for (var c0 = 0; c0 < cols; c0++) {
      var start = r0 * cols + c0;
      if (!search[start] || seen[start]) continue;
      var stack = [start]; seen[start] = 1;
      var minR = 1e9, maxR = -1, minC = 1e9, maxC = -1, any = false;
      while (stack.length) {
        var cell = stack.pop(), cr = (cell / cols) | 0, cc = cell % cols;
        if (grid[cell]) {
          any = true;
          if (cr < minR) minR = cr; if (cr > maxR) maxR = cr;
          if (cc < minC) minC = cc; if (cc > maxC) maxC = cc;
        }
        for (var dr = -1; dr <= 1; dr++) {
          for (var dc = -1; dc <= 1; dc++) {
            var nr = cr + dr, nc = cc + dc;
            if (nr < 0 || nc < 0 || nr >= rows || nc >= cols) continue;
            var ni = nr * cols + nc;
            if (search[ni] && !seen[ni]) { seen[ni] = 1; stack.push(ni); }
          }
        }
      }
      if (!any) continue;
      var x = minC * bs, y = minR * bs;
      var w = Math.min(W, (maxC + 1) * bs) - x, h = Math.min(H, (maxR + 1) * bs) - y;
      if (w * h < o.minRegionPx) continue;
      var count = 0;
      for (var yy = y; yy < y + h; yy++) {
        for (var xx = x; xx < x + w; xx++) if (mask[yy * W + xx]) count++;
      }
      out.push({ x: x, y: y, w: w, h: h, changedPixels: count, kind: "changed", shiftDy: 0, imageH: H });
    }
  }
  return out;
}

/* Inserting a row pushes everything below it down. Without recognising that
   translation the report says "the whole list changed", which is true and
   useless. Subsampled by 2 in both axes to stay interactive. */
function annotateShift(region, ga, gb, W, H) {
  var y0 = region.y, y1 = Math.min(H, region.y + region.h);
  var x0 = region.x, x1 = Math.min(W, region.x + region.w);
  if (y1 - y0 < 16 || x1 - x0 < 16) return;

  function err(dy) {
    var total = 0, count = 0;
    for (var y = y0; y < y1; y += 2) {
      var sy = y - dy;
      if (sy < 0 || sy >= H) return Infinity;
      for (var x = x0; x < x1; x += 2) {
        total += Math.abs(gb[y * W + x] - ga[sy * W + x]); count++;
      }
    }
    return count ? total / count : Infinity;
  }

  var base = err(0);
  if (!(base > 1)) return;
  var bestDy = 0, best = base;
  for (var dy = -96; dy <= 96; dy += 2) {
    if (!dy) continue;
    var e = err(dy);
    if (e < best) { best = e; bestDy = dy; }
  }
  // A match on the edge of the search window means no true alignment was
  // found: in an evenly spaced list the search locks onto a different row.
  if (Math.abs(bestDy) >= 96) return;
  if (bestDy && best < base * 0.45 && best < 12) {
    region.kind = "moved";
    region.shiftDy = bestDy;
  }
}

function positionLabel(region) {
  var c = (region.y + region.h / 2) / region.imageH;
  if (c < 0.12) return "status/nav area";
  if (c < 0.3) return "header";
  if (c < 0.7) return "mid-screen";
  return "lower screen";
}
function describe(region) {
  if (region.kind === "moved" && region.shiftDy) {
    return "moved " + (region.shiftDy > 0 ? "down" : "up") + " " + Math.abs(region.shiftDy) +
           "px (" + positionLabel(region) + ")";
  }
  return "changed (" + positionLabel(region) + ")";
}

/* ----------------------------------------------------------- renderers */

function plural(n, word) { return n + " " + word + (n === 1 ? "" : "s"); }

function css(name) {
  return getComputedStyle(document.documentElement).getPropertyValue(name).trim();
}
function rgb(hex) {
  var h = hex.replace("#", "");
  if (h.length === 3) h = h[0] + h[0] + h[1] + h[1] + h[2] + h[2];
  var v = parseInt(h, 16);
  return [(v >> 16) & 255, (v >> 8) & 255, v & 255];
}

function drawBoxes(ctx, regions, changeHex, movedHex) {
  ctx.lineWidth = 2;
  ctx.font = "600 11px ui-monospace, Menlo, monospace";
  ctx.textBaseline = "top";
  regions.forEach(function (r, i) {
    var color = r.kind === "moved" ? movedHex : changeHex;
    ctx.strokeStyle = color;
    ctx.strokeRect(r.x - 2.5, r.y - 2.5, r.w + 5, r.h + 5);
    var tag = String(i + 1);
    var tw = ctx.measureText(tag).width + 8;
    var ty = r.y - 18 > 0 ? r.y - 18 : r.y + r.h + 4;
    ctx.fillStyle = color;
    ctx.fillRect(r.x - 2.5, ty, tw, 15);
    ctx.fillStyle = "#fff";
    ctx.fillText(tag, r.x + 1.5, ty + 2);
  });
}

function renderHighlight(canvas, A, B, diff) {
  var W = diff.w, H = diff.h;
  canvas.width = W; canvas.height = H;
  var ctx = canvas.getContext("2d");
  var out = ctx.createImageData(W, H);
  var src = B.data, dst = out.data;
  var band = dilate(diff.mask, W, H, 2);
  var changeRGB = rgb(css("--change")), movedRGB = rgb(css("--moved"));
  // Wash toward the page surface, not toward white: hard-coding white turns the
  // unchanged 92% of a screenshot into the brightest thing on a dark page.
  var washRGB = rgb(css("--surface"));

  var movedMask = new Uint8Array(W * H);
  diff.regions.forEach(function (r) {
    if (r.kind !== "moved") return;
    for (var y = r.y; y < r.y + r.h && y < H; y++) {
      for (var x = r.x; x < r.x + r.w && x < W; x++) movedMask[y * W + x] = 1;
    }
  });

  for (var i = 0, j = 0; i < W * H; i++, j += 4) {
    var r0 = src[j], g0 = src[j + 1], b0 = src[j + 2];
    if (!band[i]) {                       // wash unchanged areas toward the page
      r0 += (washRGB[0] - r0) * 0.8; g0 += (washRGB[1] - g0) * 0.8; b0 += (washRGB[2] - b0) * 0.8;
    }
    if (diff.mask[i]) {                   // tint by what kind of change it is
      var t = movedMask[i] ? movedRGB : changeRGB, s = movedMask[i] ? 0.3 : 0.35;
      r0 = r0 * (1 - s) + t[0] * s; g0 = g0 * (1 - s) + t[1] * s; b0 = b0 * (1 - s) + t[2] * s;
    }
    dst[j] = r0; dst[j + 1] = g0; dst[j + 2] = b0; dst[j + 3] = 255;
  }
  ctx.putImageData(out, 0, 0);
  drawBoxes(ctx, diff.regions, css("--change"), css("--moved"));
}

function renderOverlay(canvas, A, B, diff) {
  var W = diff.w, H = diff.h;
  canvas.width = W; canvas.height = H;
  var ctx = canvas.getContext("2d");
  var out = ctx.createImageData(W, H), a = A.data, b = B.data, dst = out.data;
  var beforeRGB = rgb("#d62d3c"), afterRGB = rgb("#187aeb");

  for (var i = 0, j = 0; i < W * H; i++, j += 4) {
    var r0 = (a[j] + b[j]) / 2, g0 = (a[j + 1] + b[j + 1]) / 2, b0 = (a[j + 2] + b[j + 2]) / 2;
    if (diff.mask[i]) {
      // The darker frame owns the ink at this pixel, so shifted content reads
      // as a red ghost beside its blue counterpart.
      var la = (a[j] + a[j + 1] + a[j + 2]) / 3, lb = (b[j] + b[j + 1] + b[j + 2]) / 3;
      var t = null;
      if (la < lb - 8) t = beforeRGB; else if (lb < la - 8) t = afterRGB;
      if (t) { r0 = r0 * 0.35 + t[0] * 0.65; g0 = g0 * 0.35 + t[1] * 0.65; b0 = b0 * 0.35 + t[2] * 0.65; }
    }
    dst[j] = r0; dst[j + 1] = g0; dst[j + 2] = b0; dst[j + 3] = 255;
  }
  ctx.putImageData(out, 0, 0);
}

function renderSideBySide(canvas, A, B, diff, labels) {
  var W = diff.w, H = diff.h, gap = 18, head = 26;
  canvas.width = W * 2 + gap; canvas.height = H + head;
  var ctx = canvas.getContext("2d");
  ctx.fillStyle = css("--surface"); ctx.fillRect(0, 0, canvas.width, canvas.height);

  var tmp = document.createElement("canvas"); tmp.width = W; tmp.height = H;
  var tctx = tmp.getContext("2d");
  tctx.putImageData(A, 0, 0); ctx.drawImage(tmp, 0, head);
  tctx.putImageData(B, 0, 0); ctx.drawImage(tmp, W + gap, head);

  ctx.font = "650 12px ui-monospace, Menlo, monospace";
  ctx.textBaseline = "top";
  ctx.fillStyle = "#d62d3c"; ctx.fillText(labels[0], 2, 4);
  ctx.fillStyle = "#187aeb"; ctx.fillText(labels[1], W + gap + 2, 4);

  [0, W + gap].forEach(function (dx) {
    ctx.save(); ctx.translate(dx, head);
    drawBoxes(ctx, diff.regions, css("--change"), css("--moved"));
    ctx.restore();
  });
}

function renderSwipe(canvas, A, B, diff, split) {
  var W = diff.w, H = diff.h;
  canvas.width = W; canvas.height = H;
  var ctx = canvas.getContext("2d");
  var tmp = document.createElement("canvas"); tmp.width = W; tmp.height = H;
  var tctx = tmp.getContext("2d");

  tctx.putImageData(A, 0, 0);
  ctx.drawImage(tmp, 0, 0);
  var cut = Math.round(W * split);
  tctx.putImageData(B, 0, 0);
  ctx.save();
  ctx.beginPath(); ctx.rect(cut, 0, W - cut, H); ctx.clip();
  ctx.drawImage(tmp, 0, 0);
  ctx.restore();
}

/* ----------------------------------------------------------- overview */

/* Every number in `overview` belongs to one build pair. Anything that reads it
   calls this first, so a pair changed in the screen view can't leave the
   overview showing confident numbers for a comparison nobody is looking at. */
function freshOverview() {
  var key = ui.before + "\u2192" + ui.after;
  if (overviewPair !== key) { overview = {}; overviewPair = key; }
}

/* Every screen measured between the same two builds. On an app with one
   screen this is redundant; on a real one it is the only question worth
   asking first -- "what does this build actually look different in?" */
function paintOverview() {
  freshOverview();
  var token = ++overviewToken;
  var host = el("stage-body");
  var rows = visibleScreens();

  if (!rows.length) {
    host.innerHTML = '<p class="empty">No screens match “' + esc(ui.filter) + '”.</p>';
    return;
  }

  // Re-measuring the same set of screens keeps the cards that are already up,
  // dimmed, instead of tearing the grid down to a page of "measuring…". On a
  // 40-screen app that is over a second of empty where a full grid just was.
  var grid = el("grid");
  var reusable = !!grid && grid.querySelectorAll(".card").length === rows.length &&
    rows.every(function (s) { return !!el("card-" + s.id); });

  if (reusable) {
    rows.forEach(function (s) {
      el("card-" + s.id).dataset.stale = "true";
      var meta = el("cmeta-" + s.id);
      if (meta) meta.innerHTML = '<span class="mut">measuring…</span>';
    });
  } else {
    host.innerHTML = '<div class="grid" id="grid">' + rows.map(function (s) {
      return '<button type="button" class="card" data-screen="' + esc(s.id) + '" id="card-' + esc(s.id) + '">' +
        '<div class="thumb" id="thumb-' + esc(s.id) + '"></div>' +
        '<div class="cname">' + esc(s.name) + "</div>" +
        '<div class="cmeta" id="cmeta-' + esc(s.id) + '">measuring…</div></button>';
    }).join("") + "</div>";
    Array.prototype.forEach.call(host.querySelectorAll(".card"), function (card) {
      card.addEventListener("click", function () { openScreen(card.dataset.screen); });
    });
  }

  var i = 0;
  function step() {
    if (token !== overviewToken || ui.view !== "overview") return;
    if (i >= rows.length) { paintOverviewSummary(); applyRanking(rows); paintRail(); return; }
    measure(rows[i]).then(function (res) {
      if (token !== overviewToken || ui.view !== "overview") return;
      overview[rows[i].id] = res;
      paintCard(rows[i], res);
      i++;
      paintOverviewSummary();
      setTimeout(step, 0);
    });
  }
  step();
}

function measure(s) {
  if (overview[s.id]) return Promise.resolve(overview[s.id]);
  var before = frameFor(s, ui.before), after = frameFor(s, ui.after);
  if (!before || !after) {
    return Promise.resolve({ missing: true, inBefore: !!before, inAfter: !!after });
  }
  var W = Math.max(before.w, after.w), H = Math.max(before.h, after.h);
  return Promise.all([pixels(before, W, H), pixels(after, W, H)]).then(function (px) {
    var d = computeDiff(px[0], px[1], W, H, opts);
    var moved = d.regions.filter(function (r) { return r.kind === "moved"; }).length;
    return {
      percent: d.comparable ? (100 * d.changed / d.comparable) : 0,
      regions: d.regions.length, moved: moved, diff: d,
      after: after, afterData: px[1], missing: false
    };
  }).catch(function () { return { missing: true, inBefore: true, inAfter: true, broken: true }; });
}

function paintCard(s, res) {
  var meta = el("cmeta-" + s.id), thumb = el("thumb-" + s.id), card = el("card-" + s.id);
  if (!meta || !thumb) return;
  var st = statusOf(s.id, ui.after);
  var pill = st !== "pending" ? '<span class="pill ' + st + '">' + st + "</span>" : "";

  delete card.dataset.stale;
  if (res.missing) {
    card.dataset.state = "missing";
    // Fall back to the last frame we have: an empty box reads as a broken
    // image, where a greyed-out screenshot reads as "not in this pair".
    var f = frameFor(s, ui.after) || frameFor(s, ui.before) || s.frames[s.frames.length - 1];
    var ghost = !frameFor(s, ui.after) && !frameFor(s, ui.before) ? ' class="ghosted"' : "";
    thumb.innerHTML = f ? '<img' + ghost + ' src="' + srcOf(f) + '" alt="">' : "";
    meta.innerHTML = '<span class="mut">' +
      (res.broken ? "could not measure" : (res.inAfter ? "new in this build" : "not in this build")) +
      "</span>" + pill;
    return;
  }
  card.dataset.state = res.percent > 0 ? "changed" : "same";
  var canvas = document.createElement("canvas");
  renderHighlight(canvas, null, res.afterData, res.diff);
  thumb.innerHTML = "";
  thumb.appendChild(canvas);
  meta.innerHTML = '<b class="' + (res.percent ? "chg" : "same") + '">' + res.percent.toFixed(2) + "%</b>" +
    '<span class="mut">' + (res.regions ? (res.regions + " region" + (res.regions === 1 ? "" : "s") +
      (res.moved ? ", " + res.moved + " moved" : "")) : "identical") + "</span>" + pill;
}

/* Sorting by "most changed" can only happen once things are measured; moving
   the cards with CSS order avoids re-rendering and keeps scroll position. */
function applyRanking(rows) {
  if (ui.sort !== "change") return;
  var ranked = rows.slice().sort(function (p, q) {
    var a = overview[p.id], b = overview[q.id];
    var pa = a && !a.missing ? a.percent : -1, pb = b && !b.missing ? b.percent : -1;
    return pb !== pa ? pb - pa : p.name.localeCompare(q.name);
  });
  // FLIP: measure, reorder, then animate each card from where it used to be.
  // Without this every card in the grid jumps to a new slot on the same tick.
  var first = {};
  rows.forEach(function (s) {
    var node = el("card-" + s.id);
    if (node) first[s.id] = node.getBoundingClientRect();
  });
  ranked.forEach(function (s, index) {
    var card = el("card-" + s.id);
    if (card) card.style.order = index;
  });
  if (window.matchMedia("(prefers-reduced-motion:reduce)").matches) return;
  rows.forEach(function (s) {
    var card = el("card-" + s.id);
    if (!card || !first[s.id] || !card.animate) return;
    var last = card.getBoundingClientRect();
    var dx = first[s.id].left - last.left, dy = first[s.id].top - last.top;
    if (!dx && !dy) return;
    card.animate([{ transform: "translate(" + dx + "px," + dy + "px)" }, { transform: "none" }],
      { duration: 280, easing: "cubic-bezier(.645,.045,.355,1)" });
  });
}

function paintOverviewSummary() {
  freshOverview();
  var rows = visibleScreens();
  var done = rows.filter(function (s) { return overview[s.id]; });
  var changed = done.filter(function (s) { return overview[s.id].percent > 0; });
  var missing = done.filter(function (s) { return overview[s.id].missing; });
  var b = buildById(ui.after), a = buildById(ui.before);
  el("readout").innerHTML =
    '<div class="headline"><span class="pct' + (changed.length ? "" : " zero") + '">' +
      changed.length + "/" + rows.length + "</span>" +
      "<span>screens changed &middot; " + esc(a.label) + " → " + esc(b.label) + "</span>" +
      (missing.length ? '<span class="mut">' + missing.length + " not in both builds</span>" : "") +
      (done.length < rows.length ? '<span class="mut">measuring ' + done.length + "/" + rows.length +
        "…</span>" : "") + "</div>";
  if (ui.view === "overview") el("side").innerHTML = overviewSide();
}

function visibleScreens() {
  var q = ui.filter.trim().toLowerCase();
  var rows = DATA.screens.filter(function (s) {
    return !q || s.name.toLowerCase().indexOf(q) !== -1 ||
      (s.product_area || "").toLowerCase().indexOf(q) !== -1;
  });
  if (ui.sort === "change") {
    rows = rows.slice().sort(function (p, q2) {
      var a = overview[p.id], b = overview[q2.id];
      var pa = a && !a.missing ? a.percent : -1, pb = b && !b.missing ? b.percent : -1;
      if (pb !== pa) return pb - pa;
      return p.name.localeCompare(q2.name);
    });
  } else {
    rows = rows.slice().sort(function (p, q2) {
      return (p.product_area || "~").localeCompare(q2.product_area || "~") ||
             p.name.localeCompare(q2.name);
    });
  }
  return rows;
}

function openScreen(id) {
  ui.view = "screen";
  window.scrollTo(0, 0);
  selectScreen(id);
}

/* Anything that changes the build pair goes through here so the overview and
   the screen view can't drift apart. */
function repaint() {
  if (ui.view === "overview") {
    syncChrome(); paintRail(); paintOverview();
    el("side").innerHTML = overviewSide();
  } else {
    syncChrome(); paintRail(); paintFilm(); paintStage(); paintSide();
  }
}
function openOverview() {
  ui.view = "overview";
  ui.screen = null;
  window.scrollTo(0, 0);
  syncChrome();
  paintRail();
  paintOverview();
  el("film").innerHTML = "";
  el("side").innerHTML = overviewSide();
}

function overviewSide() {
  freshOverview();
  var b = buildById(ui.after);
  var measured = DATA.screens.filter(function (s) { return overview[s.id]; });
  var changed = measured.filter(function (s) { return overview[s.id].percent > 0; }).length;
  var missing = measured.filter(function (s) { return overview[s.id].missing; }).length;
  return "<h2>Build " + esc(b.label) + "</h2><dl class='prov'>" +
    "<dt>Commit</dt><dd>" + (b.commit_url
      ? '<a href="' + esc(b.commit_url) + '" target="_blank" rel="noopener" class="mono">' +
        esc(b.commit_short || "") + "</a>"
      : '<span class="mono">' + esc(b.commit_short || "n/a") + "</span>") + "</dd>" +
    (b.pr_url ? "<dt>PR</dt><dd><a href='" + esc(b.pr_url) + "' target='_blank' rel='noopener'>#" +
      esc(b.pr_number) + "</a></dd>" : "") +
    "<dt>Branch</dt><dd>" + esc(b.branch || "n/a") + "</dd>" +
    "<dt>Author</dt><dd>" + esc(b.author || "n/a") + "</dd>" +
    "<dt>When</dt><dd>" + esc(b.relative_time) + "</dd>" +
    "<dt>Message</dt><dd>" + esc(b.message || "") + "</dd>" +
    "<dt>Screens</dt><dd class='num'>" + DATA.screens.length + " tracked · " + changed + " changed" +
      (missing ? " · " + missing + " not in both" : "") +
      (measured.length < DATA.screens.length ? " · measuring…" : "") + "</dd>" +
    "</dl><p class='hintbox'>Pick a screen to review it, approve it, or comment on it.</p>";
}

/* ------------------------------------------------------------------ ui */

function currentPair() {
  var s = screenById(ui.screen);
  return { screen: s, before: frameFor(s, ui.before), after: frameFor(s, ui.after) };
}

var renderToken = 0;

function paintStage() {
  var token = ++renderToken;
  var pair = currentPair();
  var host = el("stage-body");
  // Both of these would otherwise diff a frame against itself and report a
  // confident 0.00%.
  if (pair.screen.frames.length === 1) {
    host.innerHTML = '<p class="empty">' + esc(pair.screen.name) + " only appears in " +
      esc(buildById(pair.screen.frames[0].build_id).label) +
      " — no other build of this screen to compare it against.</p>";
    el("readout").innerHTML = "";
    return;
  }
  if (ui.before === ui.after) {
    host.innerHTML = '<p class="empty">Both pickers are on ' + esc(buildById(ui.after).label) +
      " — choose two different builds to compare.</p>";
    el("readout").innerHTML = "";
    return;
  }
  if (!pair.before || !pair.after) {
    host.innerHTML = '<p class="empty">' + esc(pair.screen.name) +
      ' is not present in both of those builds.</p>';
    return;
  }
  var W = Math.max(pair.before.w, pair.after.w), H = Math.max(pair.before.h, pair.after.h);
  // Only say "working" if it actually takes a moment -- flashing this on every
  // fast repaint is worse than saying nothing.
  var busy = setTimeout(function () { host.dataset.busy = "true"; }, 140);
  function done() { clearTimeout(busy); delete host.dataset.busy; }
  Promise.all([pixels(pair.before, W, H), pixels(pair.after, W, H)]).then(function (px) {
    if (token !== renderToken) { done(); return; }
    var A = px[0], B = px[1];
    var diff = computeDiff(A, B, W, H, opts);
    var canvas = document.createElement("canvas");
    var bLabel = buildById(ui.before).label, aLabel = buildById(ui.after).label;

    if (ui.mode === "highlight") renderHighlight(canvas, A, B, diff);
    else if (ui.mode === "overlay") renderOverlay(canvas, A, B, diff);
    else if (ui.mode === "side-by-side") renderSideBySide(canvas, A, B, diff, [bLabel, aLabel]);
    else renderSwipe(canvas, A, B, diff, ui.swipe);

    host.innerHTML = "";
    var card = document.createElement("div");
    card.className = "canvas-card";
    if (ui.mode === "swipe") {
      var wrap = document.createElement("div");
      wrap.className = "swipe-host";
      wrap.appendChild(canvas);
      var line = document.createElement("div");
      line.className = "swipe-line";
      line.style.left = (ui.swipe * 100) + "%";
      wrap.appendChild(line);
      attachSwipe(wrap, canvas, A, B, diff, line);
      card.appendChild(wrap);
    } else {
      card.appendChild(canvas);
    }
    host.appendChild(card);
    window.__lastCanvas = canvas;
    paintReadout(diff, bLabel, aLabel);
    done();
  }).catch(function (err) {
    done();
    host.innerHTML = '<p class="empty">Could not render: ' + esc(err.message) + "</p>";
  });
}

function attachSwipe(wrap, canvas, A, B, diff, line) {
  function moveTo(t) {
    ui.swipe = Math.min(1, Math.max(0, t));
    line.style.left = (ui.swipe * 100) + "%";
    renderSwipe(canvas, A, B, diff, ui.swipe);
  }
  function move(clientX) {
    var box = canvas.getBoundingClientRect();
    moveTo((clientX - box.left) / box.width);
  }
  wrap.addEventListener("pointerdown", function (e) {
    wrap.setPointerCapture(e.pointerId); move(e.clientX);
  });
  wrap.addEventListener("pointermove", function (e) {
    if (e.buttons) move(e.clientX);
  });

  wrap.tabIndex = 0;
  wrap.setAttribute("role", "slider");
  wrap.setAttribute("aria-label", "Swipe between " + buildById(ui.before).label +
    " and " + buildById(ui.after).label);
  wrap.addEventListener("keydown", function (e) {
    var step = e.shiftKey ? 0.1 : 0.02, t = ui.swipe;
    if (e.key === "ArrowLeft") t -= step;
    else if (e.key === "ArrowRight") t += step;
    else if (e.key === "Home") t = 0;
    else if (e.key === "End") t = 1;
    else return;
    // The document handler binds the arrows to stepping through builds.
    e.preventDefault(); e.stopPropagation();
    moveTo(t);
  });
}

function paintReadout(diff, bLabel, aLabel) {
  var pct = diff.comparable ? (100 * diff.changed / diff.comparable) : 0;
  var moved = diff.regions.filter(function (r) { return r.kind === "moved"; }).length;
  var edited = diff.regions.length - moved;
  var bits = [];
  if (edited) bits.push(edited + " changed region" + (edited === 1 ? "" : "s"));
  if (moved) bits.push(moved + " moved");
  if (!bits.length) bits.push("no regions above threshold");

  var legend = "";
  if (edited) legend += '<span><i class="sw" style="background:var(--change)"></i>changed content</span>';
  if (moved) legend += '<span><i class="sw" style="background:var(--moved)"></i>same content, moved</span>';
  if (ui.mode === "overlay") {
    legend = '<span><i class="sw" style="background:#d62d3c"></i>only in ' + esc(bLabel) + "</span>" +
             '<span><i class="sw" style="background:#187aeb"></i>only in ' + esc(aLabel) + "</span>";
  }

  el("readout").innerHTML =
    '<div class="headline"><span class="pct' + (pct ? "" : " zero") + '">' + pct.toFixed(2) + "%</span>" +
      "<span>of pixels differ &middot; " + esc(bits.join(", ")) + "</span>" +
      '<span class="mono" style="color:var(--muted)">' + esc(bLabel) + " → " + esc(aLabel) + "</span></div>" +
    '<div class="legend">' + legend + "</div>" +
    '<ul class="regions">' + diff.regions.slice(0, 14).map(function (r, i) {
      // The canvas tags each box 1..N; without the same number here, seven
      // pills reading "moved down 60px" identify nothing.
      return '<li class="' + r.kind + '"><span class="ix">' + (i + 1) + "</span>" +
        esc(describe(r)) + "</li>";
    }).join("") +
    (diff.regions.length > 14
      ? '<li class="more">+' + (diff.regions.length - 14) + " more</li>" : "") + "</ul>";
}

/* Adjacent-build deltas for the filmstrip, and the first build that changed.
   Computed lazily and yielded between pairs so a long history never freezes
   the page. */
function computeTimeline(screen) {
  if (deltas[screen.id]) { paintFilm(); return; }
  var acc = {};
  deltas[screen.id] = acc;
  var i = 1;
  function step() {
    if (i >= screen.frames.length) { paintFilm(); paintBlame(); return; }
    var prev = screen.frames[i - 1], cur = screen.frames[i];
    var W = Math.max(prev.w, cur.w), H = Math.max(prev.h, cur.h);
    Promise.all([pixels(prev, W, H), pixels(cur, W, H)]).then(function (px) {
      var d = computeDiff(px[0], px[1], W, H, opts);
      acc[cur.build_id] = d.comparable ? (100 * d.changed / d.comparable) : 0;
      if (!firstChange[screen.id] && d.regions.length) firstChange[screen.id] = cur.build_id;
      i++;
      paintFilm();
      setTimeout(step, 0);
    }).catch(function () { i++; setTimeout(step, 0); });
  }
  step();
}

function paintFilm() {
  var s = screenById(ui.screen);
  el("film").innerHTML = s.frames.map(function (f, i) {
    var b = buildById(f.build_id);
    var role = f.build_id === ui.after ? "after" : (f.build_id === ui.before ? "before" : "");
    var st = statusOf(s.id, f.build_id);
    var n = commentsOf(s.id, f.build_id).length;
    var d = deltas[s.id] ? deltas[s.id][f.build_id] : undefined;
    var delta = i === 0 ? '<span class="meta">first build</span>'
      : (d === undefined ? '<span class="meta">…</span>'
        : '<span class="delta' + (d ? "" : " zero") + '">+' + d.toFixed(2) + "%</span>");
    return '<button type="button" class="frame" data-role="' + role + '" data-build="' + esc(f.build_id) +
      '" title="click = after, shift-click = before">' +
      '<img src="' + srcOf(f) + '" alt="' + esc(s.name) + " at " + esc(b.label) + '">' +
      '<div class="row"><span class="lbl">' + esc(b.label) + "</span>" +
        (role ? '<span class="role">' + role + "</span>" : "") +
        (st !== "pending" ? '<span class="pill ' + st + '">' + st + "</span>" : "") +
        (n ? '<span class="pill">' + n + "</span>" : "") + "</div>" +
      '<div class="meta">' + esc(b.message || b.version) + "</div>" +
      '<div class="row">' + delta +
        '<span class="meta">' + esc(b.relative_time) + "</span></div>" +
      "</button>";
  }).join("");

  Array.prototype.forEach.call(el("film").querySelectorAll(".frame"), function (node) {
    node.addEventListener("click", function (e) {
      if (e.shiftKey) ui.before = node.dataset.build;
      else selectAfter(node.dataset.build);
      syncChrome(); paintFilm(); paintStage(); paintSide(); paintRail();
    });
  });
}

function selectAfter(buildId) {
  var s = screenById(ui.screen);
  var idx = s.frames.findIndex(function (f) { return f.build_id === buildId; });
  ui.after = buildId;
  if (ui.before === buildId) {
    ui.before = s.frames[idx > 0 ? idx - 1 : Math.min(1, s.frames.length - 1)].build_id;
  }
}

function paintSide() {
  var s = screenById(ui.screen), b = buildById(ui.after);
  // A screen with one frame leaves the app-wide pair alone, so the panel can
  // be looking at a build this screen was never captured in.
  var frame = frameFor(s, ui.after);
  var st = statusOf(s.id, ui.after);
  var cmts = commentsOf(s.id, ui.after);

  var prov = "<h2>Build " + esc(b.label) + "</h2><dl class='prov'>" +
    "<dt>Commit</dt><dd>" + (b.commit_url
      ? '<a href="' + esc(b.commit_url) + '" target="_blank" rel="noopener" class="mono">' +
        esc(b.commit_short || "") + "</a>"
      : '<span class="mono">' + esc(b.commit_short || "n/a") + "</span>") +
      (b.dirty ? ' <span class="pill">dirty</span>' : "") + "</dd>" +
    (b.pr_url ? "<dt>PR</dt><dd><a href='" + esc(b.pr_url) + "' target='_blank' rel='noopener'>#" +
      esc(b.pr_number) + "</a></dd>" : "") +
    "<dt>Branch</dt><dd>" + esc(b.branch || "n/a") + "</dd>" +
    "<dt>Author</dt><dd>" + esc(b.author || "n/a") + "</dd>" +
    "<dt>When</dt><dd>" + esc(b.relative_time) + "</dd>" +
    "<dt>Message</dt><dd>" + esc(b.message || "") + "</dd>" +
    "<dt>Status</dt><dd><b>" + esc(st) + "</b></dd>" +
    "<dt>Frames</dt><dd class='num'>" +
      (frame ? esc(frame.observation_count) + " observations" : "not captured in this build") +
      "</dd></dl>";

  var blame = '<div id="blame-slot">' + blameHtml(s) + "</div>";

  // Nothing to sign off on when there is no frame at this build.
  var off = frame ? "" : " disabled";
  var acts = '<div class="acts"><button class="ap" data-decide="approved"' + off + ">Approve</button>" +
    '<button class="rj" data-decide="rejected"' + off + ">Reject</button></div>";

  var comments = "<h2>Comments (" + cmts.length + ")</h2>" +
    (cmts.length ? cmts.map(function (c) {
      return '<div class="cmt"><div class="who">' + esc(c.author) + " · " +
        esc((c.created_at || "").slice(0, 16).replace("T", " ")) + "</div>" + esc(c.body) + "</div>";
    }).join("") : '<p style="color:var(--muted);font-size:13px;margin:0">Nothing on this build yet.</p>') +
    '<textarea id="ta" placeholder="Comment on ' + esc(s.name) + " at " + esc(b.label) + '"></textarea>' +
    '<button class="post" id="post">Comment</button>';

  var tuning = "<h2>Detection</h2><div class='tuning'>" +
    slider("tolerance", "Tolerance", 4, 96, 1, opts.tolerance) +
    slider("minRegionPx", "Min region px", 0, 2000, 20, opts.minRegionPx) +
    slider("mergeRadius", "Merge radius", 0, 6, 1, opts.mergeRadius) +
    "<label class='chk'><input type='checkbox' id='ignoreStatusBar'" +
      (opts.ignoreStatusBar ? " checked" : "") + "> Ignore status bar</label>" +
    "<label class='chk'><input type='checkbox' id='detectShift'" +
      (opts.detectShift ? " checked" : "") + "> Detect moved content</label>" +
    "</div>";

  // paintSide() replaces the whole panel, so anything the reviewer was in the
  // middle of -- a comment, a slider they are arrowing through -- has to be
  // carried across by hand.
  var prevTa = el("ta"), draft = prevTa ? prevTa.value : "";
  var focused = document.activeElement ? document.activeElement.id : "";

  el("side").innerHTML = prov + blame + acts + comments + tuning;
  wireSide();

  var ta = el("ta");
  if (ta && draft) ta.value = draft;
  if (focused) { var again = el(focused); if (again && again.focus) again.focus(); }
}

/* The timeline finishes long after the panel is painted; swapping just this
   block keeps the sliders (and their focus) exactly where they were. */
function blameHtml(s) {
  var fc = firstChange[s.id] ? buildById(firstChange[s.id]) : null;
  if (!fc) return "";
  return '<div class="blame"><div class="t">First visual change</div><b class="mono">' +
    esc(fc.label) + "</b> — " + esc(fc.message || "") +
    (fc.author ? " · " + esc(fc.author) : "") + "</div>";
}

function paintBlame() {
  var slot = el("blame-slot");
  if (slot && ui.screen) slot.innerHTML = blameHtml(screenById(ui.screen));
}

/* One repaint per frame while dragging. rAF is the right clock for that, but
   it never fires in a hidden tab -- which silently freezes the report for
   anyone driving it from a script or a background tab -- so fall back to a
   timer when the page is not visible. */
function coalesced(run) {
  var id = 0, timer = false;
  return function () {
    if (id) { timer ? clearTimeout(id) : cancelAnimationFrame(id); }
    if (document.hidden) { timer = true; id = setTimeout(function () { id = 0; run(); }, 0); }
    else { timer = false; id = requestAnimationFrame(function () { id = 0; run(); }); }
  };
}

function slider(key, label, min, max, step, value) {
  return "<label>" + esc(label) +
    "<input type='range' id='opt-" + key + "' min='" + min + "' max='" + max +
    "' step='" + step + "' value='" + value + "'>" +
    "<span class='val' id='val-" + key + "'>" + value + "</span></label>";
}

function wireSide() {
  ["tolerance", "minRegionPx", "mergeRadius"].forEach(function (key) {
    var input = el("opt-" + key);
    if (!input) return;
    var repaintSoon = coalesced(paintStage);
    input.addEventListener("input", function () {
      opts[key] = Number(input.value);
      el("val-" + key).textContent = input.value;
      deltas = {}; firstChange = {}; overview = {}; overviewPair = "";
      repaintSoon();   // a drag fires far faster than a ~30ms diff
    });
    input.addEventListener("change", function () { computeTimeline(screenById(ui.screen)); });
  });
  ["ignoreStatusBar", "detectShift"].forEach(function (key) {
    var box = el(key);
    if (!box) return;
    box.addEventListener("change", function () {
      opts[key] = box.checked;
      deltas = {}; firstChange = {}; overview = {}; overviewPair = "";
      paintStage(); computeTimeline(screenById(ui.screen));
    });
  });
  Array.prototype.forEach.call(document.querySelectorAll("[data-decide]"), function (btn) {
    btn.addEventListener("click", function () { decide(btn.dataset.decide); });
  });
  var post = el("post");
  if (post) post.addEventListener("click", postComment);
}

function syncChrome() {
  var s = ui.screen ? screenById(ui.screen) : null;
  ["before", "after"].forEach(function (which) {
    var sel = el("sel-" + which);
    sel.innerHTML = DATA.builds.map(function (b) {
      // Flag builds this screen never appeared in rather than hiding them:
      // the pair is app-wide, and a silently short list is confusing.
      var absent = s && !frameFor(s, b.id);
      return '<option value="' + esc(b.id) + '"' + (ui[which] === b.id ? " selected" : "") + ">" +
        esc(b.label) + " · " + esc(b.message || b.version) + (absent ? "  (not in this screen)" : "") +
        "</option>";
    }).join("");
  });
  Array.prototype.forEach.call(document.querySelectorAll("#modes button"), function (btn) {
    btn.setAttribute("aria-pressed", String(btn.dataset.mode === ui.mode));
  });
  el("modes").hidden = ui.view === "overview";
  el("film-wrap").hidden = ui.view === "overview";
  el("export-png").hidden = ui.view === "overview";
  el("crumb").innerHTML = ui.view === "overview"
    ? '<span class="here">All screens</span>'
    : '<button type="button" class="link" id="to-overview">All screens</button>' +
      '<span class="sep">/</span><span class="here">' + esc(screenById(ui.screen).name) + "</span>";
  var back = el("to-overview");
  if (back) back.addEventListener("click", openOverview);
}

function selectScreen(id) {
  var s = screenById(id);
  ui.screen = id;
  // Keep the build pair when the screen has both frames, so drilling in from
  // the overview shows the comparison you were already looking at. A screen
  // with one frame adjusts nothing: the pair is app-wide, so collapsing it
  // onto that screen's only build would make every other screen read
  // "identical" the moment you looked at this one.
  if (s.frames.length > 1) {
    if (!frameFor(s, ui.after)) ui.after = s.frames[s.frames.length - 1].build_id;
    if (!frameFor(s, ui.before) || ui.before === ui.after) {
      var idx = s.frames.findIndex(function (f) { return f.build_id === ui.after; });
      if (idx > 0) ui.before = s.frames[idx - 1].build_id;
    }
    // Snapping `after` back to an older build can leave `before` newer than
    // it; keep the comparison pointing forwards in time.
    var all = DATA.builds.map(function (b) { return b.id; });
    if (all.indexOf(ui.before) > all.indexOf(ui.after)) {
      var i = all.indexOf(ui.after);
      if (i > 0) ui.before = all[i - 1];
    }
  }
  syncChrome(); paintRail(); paintFilm(); paintStage(); paintSide();
  computeTimeline(s);
}

/* The rail is the screen index: filterable, sortable, and showing each
   screen's delta for the current build pair once it has been measured. */
function paintRail() {
  freshOverview();
  var rows = visibleScreens();
  el("screens").innerHTML = rows.map(function (s) {
    var sev = severityFor(s.id), res = overview[s.id];
    var right = "";
    if (res && !res.missing) {
      right = '<span class="rdelta ' + (res.percent ? "chg" : "same") + '">' +
        res.percent.toFixed(2) + "%</span>";
    } else if (res && res.missing) {
      right = '<span class="rdelta mut">—</span>';
    }
    return '<li><button type="button" data-screen="' + esc(s.id) + '"' +
      (ui.screen === s.id ? ' aria-current="true"' : "") + ">" +
      '<span class="nm">' + esc(s.name) + right + "</span>" +
      '<span class="sub"><i class="dotm ' + sev + '"></i>' + s.frames.length + " builds" +
      (s.product_area ? " · " + esc(s.product_area) : "") + "</span></button></li>";
  }).join("");
  Array.prototype.forEach.call(el("screens").querySelectorAll("button"), function (btn) {
    btn.addEventListener("click", function () { openScreen(btn.dataset.screen); });
  });
  el("screen-count").textContent = rows.length === DATA.screens.length
    ? plural(DATA.screens.length, "screen")
    : rows.length + " of " + DATA.screens.length;
}

/* ------------------------------------------------------------- review io */

function nowISO() { return new Date().toISOString().replace(/\.\d+Z$/, "+00:00"); }
function persist() { try { localStorage.setItem(LSKEY, JSON.stringify(local)); } catch (e) {} }

function send(path, body) {
  if (!DATA.served) return Promise.resolve(false);
  return fetch(path, {
    method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body)
  }).then(function (r) { return r.ok; }).catch(function () { return false; });
}

function decide(status) {
  var rec = { screen_id: ui.screen, build_id: ui.after, status: status,
              author: DATA.user || "reviewer", decided_at: nowISO(), note: "" };
  send("/api/decide", rec).then(function (sent) {
    if (sent) {
      var f = frameFor(screenById(ui.screen), ui.after);
      if (f) f.status = status;
    } else {
      local.decisions.push(rec); persist();
    }
    paintFilm(); paintSide();
  });
}

function postComment() {
  var ta = el("ta");
  var body = (ta.value || "").trim();
  if (!body) return;
  var rec = { id: Math.random().toString(16).slice(2, 14), screen_id: ui.screen, build_id: ui.after,
              body: body, author: DATA.user || "reviewer", created_at: nowISO(),
              region: null, resolved: false };
  send("/api/comment", rec).then(function (sent) {
    if (sent) {
      var f = frameFor(screenById(ui.screen), ui.after);
      if (f) f.comments.push(rec);
    } else {
      local.comments.push(rec); persist();
    }
    paintFilm(); paintSide();
  });
}

function exportReview() {
  download(new Blob([JSON.stringify(local, null, 2)], { type: "application/json" }), "review.json");
}
function exportPng() {
  if (!window.__lastCanvas) return;
  var s = screenById(ui.screen);
  var name = s.name + "-" + buildById(ui.before).label.split(" ")[0] +
             "-to-" + buildById(ui.after).label.split(" ")[0] + "-" + ui.mode + ".png";
  var btn = el("export-png");
  btn.disabled = true; btn.textContent = "Exporting…";
  window.__lastCanvas.toBlob(function (blob) {
    download(blob, name.replace(/[#\s]/g, ""));
    btn.disabled = false; btn.textContent = "Export PNG";
  });
}
function download(blob, name) {
  var a = document.createElement("a");
  a.href = URL.createObjectURL(blob);
  a.download = name;
  a.click();
  setTimeout(function () { URL.revokeObjectURL(a.href); }, 4000);
}

/* ----------------------------------------------------------------- boot */

function severityFor(screenId) {
  var rank = { error: 0, warning: 1, info: 2 }, best = null;
  DATA.alerts.forEach(function (a) {
    if (a.screen.id !== screenId) return;
    if (best === null || rank[a.severity] < rank[best]) best = a.severity;
  });
  return best || "none";
}

function boot() {
  if (!DATA.screens.length) {
    document.body.innerHTML = '<p class="empty">No Atlas screens with screenshots for this app yet. ' +
      "Run a test against a build, then re-run <code>atlas-review report</code>.</p>";
    return;
  }
  el("app-id").textContent = DATA.app;
  el("meta").textContent = plural(DATA.screens.length, "screen") + " · " +
    plural(DATA.builds.length, "build") + " · " + DATA.generated_at;
  el("mode-note").textContent = DATA.served ? "writes to review.json" : "decisions stay in this browser";

  el("alerts").innerHTML = DATA.alerts.length ? DATA.alerts.map(function (a) {
    return '<li><span class="sev ' + a.severity + '"></span><span class="msg"><b>' +
      esc(a.screen.name) + "</b>" + esc(a.message) + "</span></li>";
  }).join("") : '<li style="color:var(--muted)">No policy alerts.</li>';

  ["before", "after"].forEach(function (which) {
    el("sel-" + which).addEventListener("change", function (e) {
      ui[which] = e.target.value;
      repaint();
    });
  });
  el("filter").addEventListener("input", function (e) {
    ui.filter = e.target.value;
    paintRail();
    if (ui.view === "overview") paintOverview();
  });
  el("sort").addEventListener("change", function (e) {
    ui.sort = e.target.value;
    paintRail();
    if (ui.view === "overview") paintOverview();
  });
  el("home").addEventListener("click", openOverview);
  Array.prototype.forEach.call(document.querySelectorAll("#modes button"), function (btn) {
    btn.addEventListener("click", function () {
      if (ui.view !== "screen") return;   // hidden, but a stray click must not throw
      ui.mode = btn.dataset.mode; syncChrome(); paintStage();
    });
  });
  el("swap").addEventListener("click", function () {
    var t = ui.before; ui.before = ui.after; ui.after = t;
    repaint();
  });
  // Theme colours are baked into the ImageData, so a flip needs a re-render.
  var dark = window.matchMedia("(prefers-color-scheme:dark)");
  if (dark.addEventListener) dark.addEventListener("change", repaint);

  el("export-png").addEventListener("click", exportPng);
  el("export-review").addEventListener("click", exportReview);

  document.addEventListener("keydown", function (e) {
    if (/^(INPUT|TEXTAREA|SELECT)$/.test(e.target.tagName)) return;
    var modes = ["highlight", "overlay", "side-by-side", "swipe"];
    if (e.key === "Escape" && ui.view === "screen") { openOverview(); e.preventDefault(); return; }
    var all = DATA.builds.map(function (b) { return b.id; });
    var idx = all.indexOf(ui.after);
    if (e.key === "ArrowRight" && idx < all.length - 1) {
      stepTo(all[idx + 1]);
    } else if (e.key === "ArrowLeft" && idx > 0) {
      stepTo(all[idx - 1]);
    } else if (/^[1-4]$/.test(e.key) && ui.view === "screen") {
      ui.mode = modes[Number(e.key) - 1];
    } else if (e.key === "s") {
      var t = ui.before; ui.before = ui.after; ui.after = t;
    } else { return; }
    e.preventDefault();
    repaint();
  });

  // Builds are app-wide, so seed the pair from the app's history, not one
  // screen's. The last two builds is the comparison people want on arrival.
  ui.after = DATA.builds[DATA.builds.length - 1].id;
  ui.before = DATA.builds[Math.max(0, DATA.builds.length - 2)].id;
  if (DATA.screens.length === 1) openScreen(DATA.screens[0].id);
  else openOverview();
}

function stepTo(buildId) {
  if (buildId === ui.before) { var t = ui.before; ui.before = ui.after; ui.after = t; return; }
  ui.after = buildId;
  var all = DATA.builds.map(function (b) { return b.id; });
  var i = all.indexOf(buildId);
  if (ui.before === buildId) ui.before = all[Math.max(0, i - 1)];
}

if (document.readyState === "loading") document.addEventListener("DOMContentLoaded", boot);
else boot();
})();
