---
name: screenhistory
description: >
  Set up and drive atlas-review (screenhistory) against a Revyl app: see every
  past iteration of every screen, diff any two builds in the browser, find the
  build where something first changed, approve screens, and gate CI on
  unexpected visual change. Use when the user says "show me past versions of
  this screen", "what changed between these builds", "screen history", "visual
  review", "visual regression on my app", "when did this button move", "who
  changed this screen", "set up atlas-review", or asks for a build-over-build
  screenshot diff report.
---

# Screen history for a mobile app

This repo turns a Revyl Atlas map into a review workflow. Atlas already stores a
screenshot of every screen for every build; `atlas-review` adds the diffing,
provenance, blame, approvals and alerting on top.

**The deliverable is one self-contained HTML file.** `atlas-review report`
inlines the raw frames and runs the detector in the browser on canvas, so the
reviewer can pick any build pair in either direction, drag the thresholds, and
switch comparison modes. Pre-rendered diffs would only answer the pairs you
guessed at. Send the file to anyone — no server, nothing to install.

`$REPO` below = the path to this cloned repo (the folder holding this SKILL.md).

## Before you start

Check these in order and stop with a clear message if one fails:

1. **The `revyl` CLI is installed and authenticated.** `revyl atlas apps` lists
   apps with their Atlas app **ids** (UUIDs, not names). Everything here needs
   an app id.
2. **The app has Atlas data for more than one build.** `revyl build list --app
   <id> --json` must return at least two versions, and the builds you care about
   must be *mapped* — a build only enters Atlas after something ran against it
   (a test run, a report, or an exploration). An unmapped build is not "no
   change"; `check` says so explicitly with a `build_not_mapped` warning, and
   the report drops it from the picker. If the recent builds are unmapped, run a
   test suite against them first (`revyl workflow run <suite> --build <id>`).
3. **Python 3.9+.** Dependencies are Pillow and numpy only.

## Setup

```bash
cd $REPO
pip install -e .            # or: pipx install .
atlas-review init --app <app-id>     # writes .atlas-review.json
```

`init` scaffolds the config: `app`, `workdir` (`.atlas-review`, where the Atlas
cache and `review.json` live), and a `policy` block. Run every command from the
directory holding that config, or pass `--config`.

Sanity-check the connection before anything else:

```bash
atlas-review builds        # build list with commit, author, time, and #N labels
atlas-review screens       # Atlas screens for this app
```

`builds` is also the fastest way to see whether git provenance came through. If
`commit`/`author` are blank, the builds were uploaded without git metadata —
diffing still works, blame just can't name a commit.

## The main flows

**Give someone the screen history.** This is the default answer to "show me past
iterations of this screen":

```bash
atlas-review report --open              # every screen, every build
atlas-review report --builds 12         # cap a long history
atlas-review report --screen checkout   # one screen
```

The report opens on an overview — every screen measured between the same build
pair, ranked by how much it moved — and drills into a filmstrip per screen.
Bound the file with `--builds`/`--screen` on a big app; it warns above 140
frames rather than silently truncating.

**Answer a specific question from the terminal:**

```bash
atlas-review changes                          # what changed in the latest build
atlas-review changes --build "#3" --markdown  # PR-comment table
atlas-review diff checkout --from "#2" --to "#5" --mode overlay -o diff.png
atlas-review blame checkout                   # first build where it changed
atlas-review blame checkout --region 0.8,0.15,0.2,0.8 --method bisect
atlas-review history checkout
```

Builds are addressable as `#N` (the ordinal shown everywhere), a version string,
a build UUID, or `latest`.

**Collect sign-off.** `atlas-review serve` runs the same UI over a local server
that writes decisions back to `.atlas-review/review.json`, which is meant to be
committed so approvals travel with the branch. A static report keeps decisions
in the browser instead; `atlas-review import review.json` merges an exported one.

**Gate CI.** `atlas-review check --markdown` evaluates the policy and exits
non-zero at or above `--fail-on` (default `error`).

## Policy, in the config file

```json
{
  "policy": {
    "compare_against": "approved",
    "default": { "max_change_pct": 2.0, "require_approval": false },
    "screens": {
      "checkout": {
        "max_change_pct": 0.5,
        "require_approval": true,
        "watch": [{ "name": "price column", "box": [0.8, 0.15, 0.2, 0.8] }]
      }
    },
    "diff": { "ignore": ["status_bar"], "tolerance": 32 }
  }
}
```

Four things worth knowing before you tune it:

- **`compare_against: "approved"` (the default) beats comparing to the previous
  build.** Against the previous build, an unreviewed change silently becomes the
  new normal after one build.
- **Approval downgrades that screen's alerts to INFO.** Without it CI stays red
  forever after an intentional redesign. A watch region with
  `"ignore_approval": true` opts out — a hard freeze.
- **`watch` boxes are fractions of the frame**, `[x, y, w, h]`, and the
  threshold is a fraction of the *box's own* area.
- **`diff.ignore` accepts `status_bar`, `ios_home_indicator`, `android_nav_bar`**
  plus arbitrary `ignore_boxes`. The status bar is masked by default because the
  clock differs on every capture.

## Things that will bite you

- **Blame answers "when did these pixels change", not "when did this widget
  appear".** In a reflowing list, a fixed band changes whenever content above it
  grows. Scope `--region` to something that does not move, or read the moved
  (amber) regions rather than the changed (red) ones.
- **Mixed capture resolutions are normal.** The same screen comes back at
  different widths from different devices; every frame of a screen is scaled to
  one width before measuring. `size_changed` means the *aspect* changed
  (rotation, different device shape) — that one is worth alerting on.
- **The CLI and the report must agree to the digit.** The detector exists twice,
  in `src/atlas_review/diff.py` and `src/atlas_review/assets/report.js`. If you
  touch either, keep them in step and extend the `JsPythonParity` tests. Frames
  are embedded byte-for-byte, never re-encoded — recompression moves the very
  number being measured.
- **Atlas coverage bounds everything.** A screen nobody's tests or explorations
  reach has no frames to compare, and shows up as "not in this build" rather
  than as a regression.

## Tests

```bash
python3 -m unittest discover -s tests    # 65 tests, no network, no revyl CLI
```
