"""`atlas-review` command line."""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path
from typing import List, Optional, Sequence

from .alerts import ERROR, Policy, post_webhook
from .api import AtlasReview
from .client import RevylError
from .diff import Box, DiffOptions

DEFAULT_CONFIG = ".atlas-review.json"


def _load_config(path: Optional[str]) -> dict:
    candidate = Path(path or DEFAULT_CONFIG)
    if not candidate.exists():
        if path:
            raise SystemExit("config not found: %s" % candidate)
        return {}
    return json.loads(candidate.read_text())


def _parse_region(text: Optional[str]) -> Optional[Box]:
    """`x,y,w,h` as fractions (0.8,0.1,0.2,0.8) or pixels with a `px` suffix."""
    if not text:
        return None
    parts = [p.strip() for p in text.replace("px", "").split(",")]
    if len(parts) != 4:
        raise SystemExit("--region wants x,y,w,h")
    values = [float(p) for p in parts]
    if any(v > 1.0 for v in values):
        raise SystemExit("--region takes fractions of the frame (0-1); got %s" % text)
    return (values[0], values[1], values[2], values[3])


def _review(args: argparse.Namespace) -> AtlasReview:
    config = _load_config(getattr(args, "config", None))
    app = getattr(args, "app", None) or config.get("app") or os.environ.get("REVYL_APP")
    if not app:
        raise SystemExit("no app: pass --app, set REVYL_APP, or add \"app\" to %s" % DEFAULT_CONFIG)
    policy = Policy.from_dict(config.get("policy", config))
    options = policy.diff_options
    for attr in ("tolerance", "block", "min_region_px"):
        value = getattr(args, attr, None)
        if value is not None:
            setattr(options, attr, value)
    if getattr(args, "no_ignore", False):
        options.ignore = []
    return AtlasReview(
        app,
        workdir=Path(getattr(args, "workdir", None) or config.get("workdir") or ".atlas-review"),
        policy=policy,
        options=options,
        repo=getattr(args, "repo", None) or config.get("repo"),
        resolve_prs=getattr(args, "prs", False) or bool(config.get("resolve_prs")),
        refresh=getattr(args, "refresh", False),
    )


# -- commands -------------------------------------------------------------


def cmd_builds(args: argparse.Namespace) -> int:
    review = _review(args)
    builds = review.builds()
    if args.json:
        print(json.dumps([b.to_dict() for b in builds], indent=2))
        return 0
    print("%d builds for %s\n" % (len(builds), review.app))
    for build in builds:
        print("  %-10s %s" % (build.label, build.title))
        if build.commit_url:
            print("             %s" % build.commit_url)
    return 0


def cmd_screens(args: argparse.Namespace) -> int:
    review = _review(args)
    screens = review.screens(args.build)
    if args.json:
        print(json.dumps([s.to_dict() for s in screens], indent=2))
        return 0
    print("%d screens\n" % len(screens))
    for screen in screens:
        print("  %-24s %-10s %s" % (screen.name, screen.short_id, screen.product_area))
    return 0


def cmd_history(args: argparse.Namespace) -> int:
    review = _review(args)
    history = review.history(args.screen)
    entries = history.timeline(review.options)
    if args.json:
        print(json.dumps(entries, indent=2))
        return 0
    print("%s — %d builds\n" % (history.screen.name, len(entries)))
    for entry in entries:
        build, diff = entry["build"], entry["diff"]
        delta = "baseline" if diff is None else ("%.2f%%  %s" % (diff["percent"], diff["summary"]))
        status = review.store.status(history.screen.id, build["id"])
        print("  %-10s %-9s %s" % (build["label"], status, delta))
        print("             %s" % build["title"])
    return 0


def cmd_diff(args: argparse.Namespace) -> int:
    review = _review(args)
    result = review.diff(args.screen, args.from_build, args.to_build)
    if args.json:
        print(json.dumps(result.to_dict(), indent=2))
    else:
        print("%s: %s" % (result.after.screen.name, result.summary))
        print("  %s -> %s" % (result.before.build.title, result.after.build.title))
        for i, region in enumerate(result.regions, start=1):
            print("  %2d. %-34s %dx%d at %d,%d" % (
                i, region.description, region.w, region.h, region.x, region.y))
    if args.out:
        out = Path(args.out)
        modes = [args.mode] if args.mode != "all" else ["side-by-side", "overlay", "highlight"]
        for mode in modes:
            path = out if len(modes) == 1 else out.with_name("%s-%s%s" % (out.stem, mode, out.suffix or ".png"))
            path.parent.mkdir(parents=True, exist_ok=True)
            result.render(mode).save(path)
            print("wrote %s" % path)
    return 0


def cmd_blame(args: argparse.Namespace) -> int:
    review = _review(args)
    region = _parse_region(args.region)
    blame = review.blame(args.screen, region=region, threshold=args.threshold, method=args.method)
    if blame is None:
        print("no change found for %s%s" % (args.screen, " in that region" if region else ""))
        return 0
    if args.json:
        print(json.dumps(blame.to_dict(), indent=2))
        return 0
    print(blame.summary)
    print("  method: %s (%d builds examined)" % (blame.method, blame.builds_examined))
    if blame.build.commit_url:
        print("  commit: %s" % blame.build.commit_url)
    if blame.build.pr_url:
        print("  PR:     %s" % blame.build.pr_url)
    if not blame.verified:
        print("  note:   change is not monotonic; scan mode will be exact")
    if args.out and blame.diff is not None:
        Path(args.out).parent.mkdir(parents=True, exist_ok=True)
        blame.diff.render(args.mode).save(args.out)
        print("  wrote %s" % args.out)
    return 0


def cmd_approve(args: argparse.Namespace) -> int:
    review = _review(args)
    fn = review.reject if args.reject else review.approve
    decision = fn(args.screen, args.build, note=args.message or "")
    print("%s %s at build %s (%s)" % (
        "rejected" if args.reject else "approved",
        review.screen(args.screen).name, review.build(args.build).label, decision.author))
    print("saved to %s" % review.store.path)
    return 0


def cmd_comment(args: argparse.Namespace) -> int:
    review = _review(args)
    entry = review.comment(
        args.screen, args.message, build=args.build, region=_parse_region(args.region)
    )
    print("comment %s on %s at %s" % (entry.id, review.screen(args.screen).name,
                                      review.build(args.build).label))
    return 0


def cmd_check(args: argparse.Namespace) -> int:
    review = _review(args)
    report = review.check(args.build, baseline=args.baseline)
    if args.json:
        print(json.dumps(report.to_dict(), indent=2))
    elif args.markdown:
        print(report.to_markdown(), end="")
    else:
        icons = {"error": "ERROR  ", "warning": "WARN   ", "info": "INFO   "}
        if not report.alerts:
            print("OK — no unexpected visual changes across %d screens" % report.screens_checked)
        for alert in report.sorted():
            print("%s%-22s %s" % (icons[alert.severity], alert.screen.name, alert.message))
    if args.webhook:
        status = post_webhook(report, args.webhook)
        print("webhook -> HTTP %d" % status, file=sys.stderr)
    return report.exit_code(args.fail_on)


def cmd_changes(args: argparse.Namespace) -> int:
    review = _review(args)
    head = review.build(args.build)
    results = review.changes(head, baseline=args.baseline, screens=args.screen or None)
    mapped = getattr(review, "last_changes_mapped", True)
    if not mapped:
        note = ("build %s has no Atlas data -- nothing was observed on it, so this is "
                "not the same as 'nothing changed'. Run a test against it first."
                % head.label)
        if args.json:
            print(json.dumps({"build": head.to_dict(), "mapped": False, "changed": []}, indent=2))
        elif args.markdown:
            print("## Visual changes in %s\n\n%s" % (head.title, note))
        else:
            print(note)
        return 0
    if args.json:
        print(json.dumps({"build": head.to_dict(),
                          "changed": [r.to_dict() for r in results]}, indent=2))
        return 0
    if args.markdown:
        print("## Visual changes in %s" % head.title)
        if head.commit_url:
            print("\n[commit](%s)%s" % (head.commit_url,
                  " · [PR #%d](%s)" % (head.pr_number, head.pr_url) if head.pr_url else ""))
        if not results:
            print("\nNo screens changed.")
            return 0
        print("\n| Screen | Change | vs | Regions |")
        print("| --- | --- | --- | --- |")
        for r in results:
            moved = sum(1 for x in r.regions if x.kind == "moved")
            print("| `%s` | %.2f%% | %s | %d changed, %d moved |" % (
                r.after.screen.name, r.percent, r.before.build.label,
                len(r.regions) - moved, moved))
        return 0
    if not results:
        print("no screens changed in %s" % head.label)
        return 0
    print("%d of %d screens changed in %s\n" % (
        len(results), len(review.screens(head)), head.title))
    for r in results:
        print("  %-26s %6.2f%%  vs %-10s %s" % (
            r.after.screen.name, r.percent, r.before.build.label, r.summary))
    return 0


def cmd_report(args: argparse.Namespace) -> int:
    review = _review(args)
    path = review.report(
        args.out, screens=args.screen or None, inline=not args.no_inline,
        open_browser=args.open, builds_limit=args.builds,
        log=lambda msg: print(msg, file=sys.stderr),
    )
    size = path.stat().st_size / 1024.0
    print("wrote %s (%.0f KB)" % (path, size))
    return 0


def cmd_serve(args: argparse.Namespace) -> int:
    from .serve import serve

    review = _review(args)
    serve(review, Path(args.out), host=args.host, port=args.port, open_browser=not args.no_open)
    return 0


def cmd_import(args: argparse.Namespace) -> int:
    review = _review(args)
    payload = json.loads(Path(args.file).read_text())
    applied = review.store.merge(payload)
    review.store.save()
    print("merged %d record(s) into %s" % (applied, review.store.path))
    return 0


def cmd_init(args: argparse.Namespace) -> int:
    path = Path(DEFAULT_CONFIG)
    if path.exists() and not args.force:
        raise SystemExit("%s already exists (use --force)" % path)
    config = {
        "app": args.app or "<revyl app id>",
        "workdir": ".atlas-review",
        "policy": {
            "compare_against": "approved",
            "default": {"max_change_pct": 2.0, "require_approval": False},
            "screens": {
                "<screen name>": {
                    "max_change_pct": 0.5,
                    "require_approval": True,
                    "watch": [{"name": "price column", "box": [0.8, 0.15, 0.2, 0.8]}],
                }
            },
            "diff": {"ignore": ["status_bar"], "tolerance": 32},
        },
    }
    path.write_text(json.dumps(config, indent=2) + "\n")
    print("wrote %s" % path)
    return 0


# -- parser ---------------------------------------------------------------


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="atlas-review",
        description="Visual review for Revyl Atlas screens: diffs, blame, approvals, alerts.",
    )
    parser.add_argument("--app", help="Revyl app id or name")
    parser.add_argument("--config", help="config file (default %s)" % DEFAULT_CONFIG)
    parser.add_argument("--workdir", help="where cache + review.json live")
    parser.add_argument("--repo", help="owner/repo for commit and PR links")
    parser.add_argument("--prs", action="store_true", help="resolve PR numbers via gh")
    parser.add_argument("--refresh", action="store_true", help="re-pull Atlas data instead of using the cache")
    parser.add_argument("--tolerance", type=int, help="per-channel pixel delta treated as noise")
    parser.add_argument("--block", type=int, help="diff block size in px")
    parser.add_argument("--min-region-px", type=int, dest="min_region_px", help="drop smaller regions")
    parser.add_argument("--no-ignore", action="store_true", help="do not mask the status bar")
    sub = parser.add_subparsers(dest="command", required=True)

    p = sub.add_parser("builds", help="list builds with commit, author and time")
    p.add_argument("--json", action="store_true")
    p.set_defaults(func=cmd_builds)

    p = sub.add_parser("screens", help="list Atlas screens")
    p.add_argument("--build", help="scope to one build")
    p.add_argument("--json", action="store_true")
    p.set_defaults(func=cmd_screens)

    p = sub.add_parser("history", help="a screen's build-by-build change history")
    p.add_argument("screen")
    p.add_argument("--json", action="store_true")
    p.set_defaults(func=cmd_history)

    p = sub.add_parser("diff", help="compare a screen between two builds")
    p.add_argument("screen")
    p.add_argument("--from", dest="from_build", help="before build (default: previous)")
    p.add_argument("--to", dest="to_build", help="after build (default: latest)")
    p.add_argument("--mode", default="highlight",
                   choices=["highlight", "side-by-side", "overlay", "mask", "all"])
    p.add_argument("-o", "--out", help="write the render to this path")
    p.add_argument("--json", action="store_true")
    p.set_defaults(func=cmd_diff)

    p = sub.add_parser("blame", help="find the first build where a screen changed")
    p.add_argument("screen")
    p.add_argument("--region", help="x,y,w,h as fractions, to scope the question")
    p.add_argument("--threshold", type=float, default=0.0, help="ignore changes below this fraction")
    p.add_argument("--method", default="scan", choices=["scan", "bisect"])
    p.add_argument("--mode", default="highlight", choices=["highlight", "side-by-side", "overlay"])
    p.add_argument("-o", "--out", help="render the culprit diff here")
    p.add_argument("--json", action="store_true")
    p.set_defaults(func=cmd_blame)

    p = sub.add_parser("approve", help="approve (or reject) a screen at a build")
    p.add_argument("screen")
    p.add_argument("--build", help="default: latest")
    p.add_argument("-m", "--message", help="note")
    p.add_argument("--reject", action="store_true")
    p.set_defaults(func=cmd_approve)

    p = sub.add_parser("comment", help="comment on a screen at a build")
    p.add_argument("screen")
    p.add_argument("-m", "--message", required=True)
    p.add_argument("--build")
    p.add_argument("--region", help="pin the comment to x,y,w,h")
    p.set_defaults(func=cmd_comment)

    p = sub.add_parser("check", help="evaluate the policy and alert on unexpected change")
    p.add_argument("--build", help="default: latest")
    p.add_argument("--baseline", help="compare against this build instead of the approved one")
    p.add_argument("--fail-on", default=ERROR, choices=["error", "warning", "info"])
    p.add_argument("--webhook", help="POST the report to this URL")
    p.add_argument("--markdown", action="store_true", help="PR-comment formatting")
    p.add_argument("--json", action="store_true")
    p.set_defaults(func=cmd_check)

    p = sub.add_parser("changes", help="every screen that changed in a build")
    p.add_argument("--build", help="default: latest")
    p.add_argument("--baseline", help="compare against this build instead of the approved one")
    p.add_argument("--screen", action="append", help="limit to these screens")
    p.add_argument("--markdown", action="store_true", help="PR-comment table")
    p.add_argument("--json", action="store_true")
    p.set_defaults(func=cmd_changes)

    p = sub.add_parser("report", help="build the HTML review report")
    p.add_argument("-o", "--out", default="atlas-review-report")
    p.add_argument("--screen", action="append", help="limit to these screens")
    p.add_argument("--no-inline", action="store_true", help="write assets alongside the HTML")
    p.add_argument("--open", action="store_true", help="open in a browser")
    p.add_argument("--builds", type=int, help="include only the last N builds")
    p.set_defaults(func=cmd_report)

    p = sub.add_parser("serve", help="serve the report so approvals write back")
    p.add_argument("-o", "--out", default=".atlas-review/served")
    p.add_argument("--host", default="127.0.0.1")
    p.add_argument("--port", type=int, default=7391)
    p.add_argument("--no-open", action="store_true")
    p.set_defaults(func=cmd_serve)

    p = sub.add_parser("import", help="merge a review.json exported from a static report")
    p.add_argument("file")
    p.set_defaults(func=cmd_import)

    p = sub.add_parser("init", help="write a starter config")
    p.add_argument("--app")
    p.add_argument("--force", action="store_true")
    p.set_defaults(func=cmd_init)
    return parser


def main(argv: Optional[Sequence[str]] = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        return int(args.func(args) or 0)
    except RevylError as exc:
        print("error: %s" % exc, file=sys.stderr)
        return 2
    except (ValueError, FileNotFoundError) as exc:
        print("error: %s" % exc, file=sys.stderr)
        return 2
    except KeyboardInterrupt:
        return 130


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
