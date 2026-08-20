"""Unit tests. No network and no revyl CLI -- images and payloads are synthetic."""

from __future__ import annotations

import datetime as _dt
import json
import sys
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from PIL import Image, ImageDraw  # noqa: E402

from atlas_review import (  # noqa: E402
    Build,
    DiffOptions,
    Policy,
    ReviewStore,
    Screen,
    ScreenVersion,
    compare_images,
    diff_versions,
)
from atlas_review.alerts import ERROR, INFO, ScreenPolicy, WatchRegion  # noqa: E402
from atlas_review.models import _humanize, _parse_ts, builds_through  # noqa: E402

W, H = 200, 400


def frame(marks=(), bg=(255, 255, 255), clock="12:00") -> Image.Image:
    """A fake phone frame: a status-bar clock plus optional filled boxes."""
    img = Image.new("RGB", (W, H), bg)
    draw = ImageDraw.Draw(img)
    draw.text((8, 6), clock, fill=(0, 0, 0))
    for (x, y, w, h, color) in marks:
        draw.rectangle([x, y, x + w, y + h], fill=color)
    return img


class DiffDetection(unittest.TestCase):
    def test_identical_frames_have_no_change(self):
        img = frame([(20, 100, 60, 40, (10, 10, 10))])
        _, regions, changed, _, resized = compare_images(img, img)
        self.assertEqual(regions, [])
        self.assertEqual(changed, 0)
        self.assertFalse(resized)

    def test_status_bar_clock_is_ignored_by_default(self):
        a = frame(clock="12:00")
        b = frame(clock="03:47")
        _, regions, changed, _, _ = compare_images(a, b)
        self.assertEqual(regions, [], "status bar must not register as a change")
        self.assertEqual(changed, 0)

    def test_clock_registers_when_masking_is_off(self):
        a = frame(clock="12:00")
        b = frame(clock="03:47")
        _, regions, _, _, _ = compare_images(a, b, DiffOptions(ignore=[]))
        self.assertTrue(regions, "without the mask the clock should be detected")

    def test_real_change_is_detected_and_located(self):
        a = frame()
        b = frame([(40, 150, 70, 50, (200, 20, 20))])
        _, regions, changed, _, _ = compare_images(a, b)
        self.assertEqual(len(regions), 1)
        region = regions[0]
        self.assertGreater(changed, 1000)
        self.assertLessEqual(region.x, 40)
        self.assertGreaterEqual(region.x + region.w, 110)
        self.assertEqual(region.kind, "changed")

    def test_translated_content_is_reported_as_moved(self):
        marks = [(30, 120, 90, 30, (20, 20, 20)), (30, 200, 90, 30, (20, 20, 20))]
        shifted = [(x, y + 40, w, h, c) for (x, y, w, h, c) in marks]
        _, regions, _, _, _ = compare_images(frame(marks), frame(shifted))
        self.assertTrue(regions)
        moved = [r for r in regions if r.kind == "moved"]
        self.assertTrue(moved, "a pure translation should be labelled moved")
        self.assertTrue(all(r.shift_dy > 0 for r in moved), "shift should read as downward")

    def test_size_change_is_flagged(self):
        a = frame()
        b = frame().resize((W, H + 60))
        _, _, _, _, resized = compare_images(a, b)
        self.assertTrue(resized)

    def test_speckle_below_the_density_floor_is_ignored(self):
        a = frame()
        b = a.copy()
        b.putpixel((100, 200), (0, 0, 0))
        b.putpixel((150, 300), (0, 0, 0))
        _, regions, _, _, _ = compare_images(a, b)
        self.assertEqual(regions, [], "isolated pixels are compression noise, not change")


class RegionQueries(unittest.TestCase):
    def setUp(self):
        self.tmp = TemporaryDirectory()
        root = Path(self.tmp.name)
        self.a_path, self.b_path = root / "a.png", root / "b.png"
        frame().save(self.a_path)
        # One box on the left, one on the right.
        frame([(10, 150, 50, 40, (0, 0, 0)), (150, 300, 40, 40, (0, 0, 0))]).save(self.b_path)
        screen = Screen(id="s1", name="home")
        self.before = ScreenVersion(screen, _build(1), image_path=self.a_path)
        self.after = ScreenVersion(screen, _build(2), image_path=self.b_path)
        self.result = diff_versions(self.before, self.after)

    def tearDown(self):
        self.tmp.cleanup()

    def test_changed_in_counts_only_pixels_inside_the_box(self):
        left = self.result.changed_in((0.0, 0.0, 0.5, 1.0))
        right = self.result.changed_in((0.5, 0.0, 0.5, 1.0))
        self.assertGreater(left, 0)
        self.assertGreater(right, 0)
        self.assertEqual(left + right, self.result.changed_pixels)

    def test_empty_box_reports_no_change(self):
        self.assertEqual(self.result.changed_in((0.0, 0.55, 0.4, 0.1)), 0)

    def test_fraction_is_relative_to_the_box(self):
        box = (0.0, 0.35, 0.35, 0.15)
        fraction = self.result.changed_fraction_in(box)
        self.assertGreater(fraction, 0.0)
        self.assertLessEqual(fraction, 1.0)

    def test_renderers_all_produce_images(self):
        for mode in ("highlight", "side-by-side", "overlay", "mask"):
            img = self.result.render(mode)
            self.assertGreater(img.width, 0, mode)
            self.assertGreater(img.height, 0, mode)

    def test_unknown_mode_raises(self):
        with self.assertRaises(ValueError):
            self.result.render("nope")


def _build(n: int, **kwargs) -> Build:
    defaults = dict(
        id="%08d-0000-0000-0000-000000000000" % n,
        version="main-v%d" % n,
        ordinal=n,
        commit="%040d" % n,
        commit_short="%07d" % n,
        branch="main",
        message="v%d: change number %d" % (n, n),
        author="ethan zhou",
        committed_at=_dt.datetime(2026, 8, 13, 12, n, tzinfo=_dt.timezone.utc),
        uploaded_at=_dt.datetime(2026, 8, 13, 12, n, tzinfo=_dt.timezone.utc),
        remote="git@github.com:acme/app.git",
    )
    defaults.update(kwargs)
    return Build(**defaults)


class BuildNaming(unittest.TestCase):
    def test_label_is_ordinal_and_commit(self):
        self.assertEqual(_build(4).label, "#4 0000004")

    def test_title_carries_branch_message_and_age(self):
        title = _build(4).title
        self.assertIn("#4", title)
        self.assertIn("main", title)
        self.assertIn("change number 4", title)

    def test_long_subject_is_truncated(self):
        build = _build(1, message="x" * 200)
        self.assertLess(len(build.title), 140)

    def test_commit_and_pr_urls_from_ssh_remote(self):
        build = _build(2, pr_number=17)
        self.assertEqual(build.commit_url, "https://github.com/acme/app/commit/%040d" % 2)
        self.assertEqual(build.pr_url, "https://github.com/acme/app/pull/17")

    def test_https_remote_parses_too(self):
        build = _build(3, remote="https://github.com/acme/app.git")
        self.assertTrue(build.commit_url.startswith("https://github.com/acme/app/commit/"))

    def test_missing_remote_yields_no_links(self):
        build = _build(3, remote=None)
        self.assertIsNone(build.commit_url)
        self.assertIsNone(build.pr_url)

    def test_dirty_tree_is_marked(self):
        self.assertIn("*", _build(3, dirty=True).title)

    def test_from_api_reads_the_git_block(self):
        payload = {
            "id": "abc", "version": "main-1", "uploaded_at": "2026-08-13T22:33:39.782113Z",
            "metadata": {"git": {"commit": "a" * 40, "commit_short": "aaaaaaa", "branch": "main",
                                 "message": "hello", "author": "someone",
                                 "timestamp": "2026-08-13 15:33:37 -0700"}},
        }
        build = Build.from_api(payload, ordinal=1)
        self.assertEqual(build.author, "someone")
        self.assertEqual(build.branch, "main")
        self.assertIsNotNone(build.committed_at)

    def test_timestamp_parsing_shapes(self):
        self.assertIsNotNone(_parse_ts("2026-08-13T22:33:39.782113Z"))
        self.assertIsNotNone(_parse_ts("2026-08-13 15:33:37 -0700"))
        self.assertIsNone(_parse_ts(None))
        self.assertIsNone(_parse_ts("not a date"))

    def test_humanize_handles_missing_time(self):
        self.assertEqual(_humanize(None), "unknown time")


class Review(unittest.TestCase):
    def setUp(self):
        self.tmp = TemporaryDirectory()
        self.store = ReviewStore(Path(self.tmp.name) / "review.json")

    def tearDown(self):
        self.tmp.cleanup()

    def test_default_status_is_pending(self):
        self.assertEqual(self.store.status("s1", "b1"), "pending")

    def test_approve_then_read_back_from_disk(self):
        self.store.approve("s1", "b1", author="ethan", note="fine")
        path = self.store.save()
        reloaded = ReviewStore(path)
        self.assertEqual(reloaded.status("s1", "b1"), "approved")
        self.assertEqual(reloaded.decision("s1", "b1").note, "fine")

    def test_latest_decision_wins(self):
        self.store.approve("s1", "b1")
        self.store.reject("s1", "b1")
        self.assertEqual(self.store.status("s1", "b1"), "rejected")

    def test_baseline_is_the_most_recent_approved_build(self):
        self.store.approve("s1", "b1")
        self.store.approve("s1", "b3")
        self.store.reject("s1", "b4")
        self.assertEqual(self.store.baseline_build_id("s1", ["b1", "b2", "b3", "b4"]), "b3")

    def test_baseline_is_none_without_approvals(self):
        self.assertIsNone(self.store.baseline_build_id("s1", ["b1", "b2"]))

    def test_invalid_status_rejected(self):
        with self.assertRaises(ValueError):
            self.store.decide("s1", "b1", "maybe")

    def test_comments_are_scoped_and_countable(self):
        self.store.comment("s1", "b1", "first", author="a")
        self.store.comment("s1", "b2", "second", author="b")
        self.assertEqual(len(self.store.comments_for("s1")), 2)
        self.assertEqual(len(self.store.comments_for("s1", "b1")), 1)

    def test_resolving_a_comment_hides_it_from_the_open_count(self):
        entry = self.store.comment("s1", "b1", "nit", author="a")
        self.assertEqual(self.store.open_comment_count("s1", "b1"), 1)
        self.store.resolve(entry.id)
        self.assertEqual(self.store.open_comment_count("s1", "b1"), 0)

    def test_region_anchored_comment_round_trips(self):
        self.store.comment("s1", "b1", "here", region=(0.1, 0.2, 0.3, 0.4))
        path = self.store.save()
        reloaded = ReviewStore(path)
        self.assertEqual(reloaded.comments_for("s1")[0].region, [0.1, 0.2, 0.3, 0.4])

    def test_merge_is_idempotent_for_comments(self):
        self.store.comment("s1", "b1", "hello")
        payload = self.store.to_dict()
        before = len(self.store.comments)
        self.store.merge(payload)
        self.assertEqual(len(self.store.comments), before)

    def test_merge_imports_new_decisions(self):
        other = {"decisions": [{"screen_id": "s9", "build_id": "b9", "status": "approved",
                                "author": "x", "decided_at": "2026-08-14T00:00:00+00:00",
                                "note": ""}], "comments": []}
        self.assertEqual(self.store.merge(other), 1)
        self.assertEqual(self.store.status("s9", "b9"), "approved")

    def test_corrupt_store_does_not_explode(self):
        path = Path(self.tmp.name) / "bad.json"
        path.write_text("{not json")
        self.assertEqual(ReviewStore(path).status("s", "b"), "pending")


class PolicyLoading(unittest.TestCase):
    def test_defaults_apply_to_unlisted_screens(self):
        policy = Policy.from_dict({"default": {"max_change_pct": 3.0}})
        self.assertEqual(policy.for_screen("anything").max_change_pct, 3.0)

    def test_screen_rules_override_defaults(self):
        policy = Policy.from_dict({
            "default": {"max_change_pct": 3.0},
            "screens": {"home": {"max_change_pct": 0.5, "require_approval": True}},
        })
        self.assertEqual(policy.for_screen("home").max_change_pct, 0.5)
        self.assertTrue(policy.for_screen("home").require_approval)
        self.assertEqual(policy.for_screen("other").max_change_pct, 3.0)

    def test_watch_regions_parse(self):
        policy = Policy.from_dict({
            "screens": {"home": {"watch": [{"name": "logo", "box": [0, 0, 0.3, 0.1],
                                            "ignore_approval": True}]}},
        })
        watch = policy.for_screen("home").watch[0]
        self.assertIsInstance(watch, WatchRegion)
        self.assertEqual(watch.name, "logo")
        self.assertTrue(watch.ignore_approval)

    def test_diff_options_come_from_the_policy(self):
        policy = Policy.from_dict({"diff": {"tolerance": 50, "ignore": []}})
        self.assertEqual(policy.diff_options.tolerance, 50)
        self.assertEqual(list(policy.diff_options.ignore), [])

    def test_policy_file_round_trip(self):
        with TemporaryDirectory() as tmp:
            path = Path(tmp) / "p.json"
            path.write_text(json.dumps({"default": {"max_change_pct": 1.5}}))
            self.assertEqual(Policy.load(path).default.max_change_pct, 1.5)

    def test_screen_policy_defaults_are_sane(self):
        rules = ScreenPolicy()
        self.assertFalse(rules.require_approval)
        self.assertGreater(rules.max_change_pct, 0)


class AlertReporting(unittest.TestCase):
    def _report(self):
        from atlas_review.alerts import Alert, AlertReport

        screen = Screen(id="s1", name="home")
        report = AlertReport(app="app", build=_build(2), screens_checked=1)
        report.alerts.append(Alert(severity=ERROR, code="c", screen=screen, build=_build(2),
                                   message="something broke"))
        report.alerts.append(Alert(severity=INFO, code="d", screen=screen, build=_build(2),
                                   message="fyi"))
        return report

    def test_exit_code_reflects_severity(self):
        report = self._report()
        self.assertEqual(report.exit_code("error"), 1)
        self.assertFalse(report.ok)

    def test_info_only_report_passes_on_error_gate(self):
        report = self._report()
        report.alerts = [a for a in report.alerts if a.severity == INFO]
        self.assertEqual(report.exit_code("error"), 0)
        self.assertTrue(report.ok)

    def test_markdown_includes_commit_link_and_messages(self):
        text = self._report().to_markdown()
        self.assertIn("something broke", text)
        self.assertIn("commit", text)

    def test_empty_report_says_so(self):
        from atlas_review.alerts import AlertReport

        text = AlertReport(app="a", build=_build(1), screens_checked=4).to_markdown()
        self.assertIn("No unexpected visual changes", text)

    def test_errors_sort_before_info(self):
        self.assertEqual(self._report().sorted()[0].severity, ERROR)


class BuildOrdering(unittest.TestCase):
    """`uploaded_at` is optional and inconsistently shaped; ordering must not use it."""

    def test_missing_timestamp_does_not_crash(self):
        builds = [_build(1), _build(2, uploaded_at=None), _build(3)]
        self.assertEqual([b.ordinal for b in builds_through(builds, builds[-1])], [1, 2, 3])

    def test_naive_and_aware_timestamps_mix_freely(self):
        naive = _dt.datetime(2026, 8, 13, 12, 2)  # what "%Y-%m-%d %H:%M:%S" parses to
        builds = [_build(1), _build(2, uploaded_at=naive), _build(3)]
        self.assertEqual([b.ordinal for b in builds_through(builds, builds[-1])], [1, 2, 3])

    def test_head_is_always_last_in_the_slice(self):
        builds = [_build(1), _build(2), _build(3)]
        order = builds_through(builds, builds[1])
        self.assertEqual([b.ordinal for b in order], [1, 2])
        self.assertEqual(order[-1].id, builds[1].id)

    def test_unknown_head_falls_back_to_the_whole_history(self):
        builds = [_build(1), _build(2)]
        self.assertEqual(len(builds_through(builds, _build(9))), 2)


class JsPythonParity(unittest.TestCase):
    """The report re-implements the detector in JS; the constants must match.

    A CI gate reading 1.00% while the report shows 0.98% destroys trust in
    both, and the failure is invisible without a check like this.
    """

    def setUp(self):
        self.js = (Path(__file__).resolve().parents[1]
                   / "src" / "atlas_review" / "assets" / "report.js").read_text()

    def test_masks_are_floored_like_python(self):
        # Python slices masks with int(); JS must floor, never round.
        self.assertIn("Math.floor(box[1] * H)", self.js)
        self.assertIn("Math.floor((box[1] + box[3]) * H)", self.js)
        self.assertNotIn("Math.round(box", self.js)

    def test_every_mask_travels_to_the_browser(self):
        """A policy masking the nav bar must mask it in the report too."""
        from atlas_review.diff import DiffOptions
        from atlas_review.report import _policy_masks

        options = DiffOptions(ignore=("status_bar", "android_nav_bar"),
                              ignore_boxes=[(0.0, 0.5, 1.0, 0.1)])
        boxes = _policy_masks(options)
        # status_bar is the checkbox's job; everything else has to be sent.
        self.assertEqual(len(boxes), 2)
        self.assertIn([0.0, 0.95, 1.0, 0.05], boxes)
        self.assertIn([0.0, 0.5, 1.0, 0.1], boxes)
        self.assertIn("ignoreBoxes", self.js)

    def test_block_density_floor_uses_floor_like_python(self):
        # Python: max(1, int(density * block * block))
        self.assertIn("Math.floor(o.minBlockDensity * bs * bs)", self.js)

    def test_status_bar_preset_fraction_matches(self):
        from atlas_review.diff import IGNORE_PRESETS

        self.assertEqual(IGNORE_PRESETS["status_bar"], (0.0, 0.0, 1.0, 0.07))
        self.assertIn("[[0, 0, 1, 0.07]]", self.js)

    def test_shift_thresholds_match(self):
        # _detect_shift: max_shift 96, min_gain 0.45, absolute floor 12.0
        self.assertIn("dy <= 96", self.js)
        self.assertIn("base * 0.45", self.js)
        self.assertIn("best < 12", self.js)

    def test_edge_of_range_shift_is_rejected_in_both(self):
        self.assertIn("Math.abs(bestDy) >= 96", self.js)
        source = (Path(__file__).resolve().parents[1]
                  / "src" / "atlas_review" / "diff.py").read_text()
        self.assertIn("abs(best_dy) >= max_shift", source)

    def test_defaults_are_forwarded_to_the_report(self):
        from atlas_review.diff import DiffOptions
        from atlas_review import report as report_mod

        source = Path(report_mod.__file__).read_text()
        for key in ("tolerance", "minRegionPx", "mergeRadius", "minBlockDensity"):
            self.assertIn(key, source, "report payload must carry %s" % key)
        self.assertEqual(DiffOptions().tolerance, 32)


class ShiftEdgeCases(unittest.TestCase):
    def test_evenly_spaced_rows_do_not_fake_a_huge_shift(self):
        """A repeating list must not report a bogus move at the search bound."""
        rows = [(20, 60 + i * 40, 120, 20, (20, 20, 20)) for i in range(8)]
        # Recolour one row instead of moving anything: nothing has translated.
        edited = list(rows)
        edited[3] = (20, 60 + 3 * 40, 120, 20, (200, 30, 30))
        _, regions, _, _, _ = compare_images(frame(rows), frame(edited))
        for region in regions:
            self.assertLess(abs(region.shift_dy), 96,
                            "edge-of-window match should not be reported as a move")


class ScaleNormalisation(unittest.TestCase):
    """Atlas returns whatever resolution the run produced; 1x vs 3x must not
    read as a redesign."""

    def test_same_content_at_different_scales_is_not_a_change(self):
        marks = [(30, 120, 90, 40, (20, 20, 20)), (30, 220, 60, 30, (200, 40, 40))]
        base = frame(marks)
        retina = base.resize((W * 3, H * 3), Image.LANCZOS)
        _, regions, changed, comparable, aspect = compare_images(base, retina)
        self.assertFalse(aspect, "same aspect ratio must not flag as a size change")
        self.assertLess(100.0 * changed / comparable, 2.0,
                        "a pure rescale should be near-identical, not a redesign")

    def test_padding_would_have_been_catastrophic(self):
        """Guards the bug this fixes: union-padding different scales reads ~90%.

        Uses a filled background, like a real screenshot -- padding a small
        frame onto a big white canvas is only harmless when the content is
        already white.
        """
        from atlas_review import imaging

        base = frame([(30, 120, 90, 40, (20, 20, 20))], bg=(48, 92, 190))
        retina = base.resize((W * 3, H * 3), Image.LANCZOS)
        padded = imaging.fit_to(base, retina.size)
        _, _, changed, comparable, _ = compare_images(padded, retina, DiffOptions(detect_shift=False))
        self.assertGreater(100.0 * changed / comparable, 50.0,
                           "padding mismatched scales should look like a total rewrite")

    def test_filled_background_survives_rescale(self):
        base = frame([(30, 120, 90, 40, (20, 20, 20))], bg=(48, 92, 190))
        retina = base.resize((W * 3, H * 3), Image.LANCZOS)
        _, _, changed, comparable, _ = compare_images(base, retina)
        self.assertLess(100.0 * changed / comparable, 2.0)

    def test_real_aspect_change_is_still_flagged(self):
        portrait = frame()
        landscape = frame().resize((H, W))
        _, _, _, _, aspect = compare_images(portrait, landscape)
        self.assertTrue(aspect, "a rotated frame is a genuine shape change")

    def test_change_survives_rescaling(self):
        before = frame([(30, 120, 90, 40, (20, 20, 20))])
        after = frame([(30, 120, 90, 40, (20, 20, 20)), (30, 260, 90, 40, (200, 30, 30))])
        _, regions, _, _, _ = compare_images(before, after.resize((W * 2, H * 2), Image.LANCZOS))
        self.assertTrue(regions, "a real edit must still be found across scales")

    def test_normalize_pair_targets_the_smaller_width(self):
        from atlas_review.diff import normalize_pair

        a, b, _ = normalize_pair(frame(), frame().resize((W * 3, H * 3)))
        self.assertEqual(a.width, W)
        self.assertEqual(b.width, W)


class ReportCliAgreement(unittest.TestCase):
    """The report and the CLI must measure the same pixels."""

    def test_normalisation_cap_matches_the_report(self):
        import inspect

        from atlas_review import report as report_mod
        from atlas_review.timeline import MAX_FRAME_WIDTH

        default = inspect.signature(report_mod.build_report).parameters["max_width"].default
        self.assertEqual(
            default, MAX_FRAME_WIDTH,
            "report embeds frames at max_width; the detector normalises to "
            "MAX_FRAME_WIDTH -- if they diverge the CLI and the page disagree")

    def test_screen_wide_target_is_pair_independent(self):
        """Comparing 2-vs-3 must normalise like 1-vs-3, or numbers shift per pair."""
        from atlas_review.diff import normalize_pair

        small, big = frame(), frame().resize((W * 3, H * 3))
        a, b, _ = normalize_pair(small, big, 120)
        self.assertEqual((a.width, b.width), (120, 120))


if __name__ == "__main__":
    unittest.main(verbosity=2)
