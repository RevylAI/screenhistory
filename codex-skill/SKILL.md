---
name: screenhistory
description: >
  Set up and drive atlas-review (screenhistory) against a Revyl app: see every
  past iteration of every screen, diff any two builds in the browser, find the
  build where something first changed, approve screens, and gate CI on
  unexpected visual change. Trigger on "show me past versions of this screen",
  "what changed between these builds", "screen history", "visual review",
  "visual regression", "when did this button move", or "set up atlas-review".
---

# Screen history for a mobile app (Codex)

This repo turns a Revyl Atlas map into a review workflow. Atlas stores a
screenshot of every screen for every build; `atlas-review` adds diffing,
provenance, blame, approvals and alerting.

The deliverable is **one self-contained HTML file**: `atlas-review report`
inlines the raw frames and runs the detector in the browser, so any build pair
can be compared in either direction with adjustable thresholds. `$REPO` = this
cloned repo's path.

## Prerequisites — check first, stop clearly if missing

- `revyl` CLI installed and authenticated. `revyl atlas apps` lists apps with
  their Atlas app **ids** (UUIDs, not names).
- At least two *mapped* builds: `revyl build list --app <id> --json`. A build
  enters Atlas only after something ran against it. An unmapped build is not
  "no change" — `check` emits `build_not_mapped`. Fix by running a suite:
  `revyl workflow run <suite> --build <id>`.
- Python 3.9+. Dependencies: Pillow, numpy.

## Setup

```bash
cd $REPO
pip install -e .
atlas-review init --app <app-id>     # writes .atlas-review.json
atlas-review builds                  # verify: #N labels, commit, author, time
atlas-review screens
```

Run commands from the directory holding `.atlas-review.json`, or pass
`--config`. Builds are addressable as `#N`, a version string, a UUID, or
`latest`.

## Common tasks

```bash
atlas-review report --open                      # the screen-history report
atlas-review report --builds 12                 # cap a long history
atlas-review changes --markdown                 # PR table of what moved
atlas-review diff checkout --from "#2" --to "#5" --mode overlay -o diff.png
atlas-review blame checkout --region 0.8,0.15,0.2,0.8 --method bisect
atlas-review serve                              # approvals write to review.json
atlas-review check --markdown                   # CI gate; non-zero at --fail-on
```

## Policy (in `.atlas-review.json`)

- `compare_against: "approved"` (default) beats comparing to the previous
  build — otherwise an unreviewed change becomes the new normal after one build.
- Approving a screen at a build downgrades its alerts to INFO, so an intentional
  redesign does not keep CI red. `"ignore_approval": true` on a watch region
  opts out.
- `watch` boxes are `[x, y, w, h]` as fractions of the frame; the threshold is a
  fraction of the box's own area.
- `diff.ignore` takes `status_bar` (default), `ios_home_indicator`,
  `android_nav_bar`, plus arbitrary `ignore_boxes`.

## Gotchas

- Blame answers "when did these pixels change", not "when did this widget
  appear" — in a reflowing list a fixed band changes whenever content above it
  grows.
- Frames of one screen arrive at different widths from different devices; all
  are normalised to one width before measuring. `size_changed` means the aspect
  changed (rotation / device shape).
- The detector exists twice — `src/atlas_review/diff.py` and
  `src/atlas_review/assets/report.js`. Change one, change both, and extend the
  `JsPythonParity` tests. Frames are embedded byte-for-byte; never re-encode.
- Coverage bounds the answer: a screen no test or exploration reaches has no
  frames to compare.

## Tests

```bash
python3 -m unittest discover -s tests    # 65 tests, no network, no revyl CLI
```
