"""Policy-driven alerting on visual change.

A raw diff percentage is not an alert. What makes a change worth waking someone
for is context: was it approved, does it touch a region that is supposed to be
frozen, is it a screen that vanished entirely. The policy file encodes that,
and `check()` turns a build comparison into a list of alerts with severities a
CI job can act on.

Compare-against defaults to the last human-approved build rather than the
previous build, so a change that was never signed off keeps alerting instead of
becoming the new normal after one build.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence, Tuple

from .client import AtlasClient
from .diff import Box, DiffOptions, DiffResult, diff_versions
from .models import Build, Screen
from .review import APPROVED, ReviewStore
from .timeline import ScreenHistory

ERROR = "error"
WARNING = "warning"
INFO = "info"
_RANK = {ERROR: 0, WARNING: 1, INFO: 2}


@dataclass
class WatchRegion:
    name: str
    box: Box
    max_change_pct: float = 0.0
    severity: str = ERROR
    # True = a hard freeze that keeps alerting even after a human approves the
    # build. Use for areas that must never move (logo, legal copy, price).
    ignore_approval: bool = False


@dataclass
class ScreenPolicy:
    max_change_pct: float = 2.0
    require_approval: bool = False
    severity: str = WARNING
    watch: List[WatchRegion] = field(default_factory=list)
    alert_on_new: bool = True
    alert_on_removed: bool = True

    @classmethod
    def from_dict(cls, raw: Dict[str, Any], base: Optional["ScreenPolicy"] = None) -> "ScreenPolicy":
        base = base or cls()
        watch = [
            WatchRegion(
                name=w.get("name", "watched region"),
                box=tuple(w["box"]),  # type: ignore[arg-type]
                max_change_pct=float(w.get("max_change_pct", 0.0)),
                severity=w.get("severity", ERROR),
                ignore_approval=bool(w.get("ignore_approval", False)),
            )
            for w in raw.get("watch", [])
            if w.get("box")
        ]
        return cls(
            max_change_pct=float(raw.get("max_change_pct", base.max_change_pct)),
            require_approval=bool(raw.get("require_approval", base.require_approval)),
            severity=raw.get("severity", base.severity),
            watch=watch or list(base.watch),
            alert_on_new=bool(raw.get("alert_on_new", base.alert_on_new)),
            alert_on_removed=bool(raw.get("alert_on_removed", base.alert_on_removed)),
        )


@dataclass
class Policy:
    """Rules for what counts as an alert-worthy visual change."""

    default: ScreenPolicy = field(default_factory=ScreenPolicy)
    screens: Dict[str, ScreenPolicy] = field(default_factory=dict)
    compare_against: str = "approved"      # "approved" | "previous"
    diff_options: DiffOptions = field(default_factory=DiffOptions)

    @classmethod
    def load(cls, path: Path) -> "Policy":
        raw = json.loads(Path(path).read_text())
        return cls.from_dict(raw)

    @classmethod
    def from_dict(cls, raw: Dict[str, Any]) -> "Policy":
        default = ScreenPolicy.from_dict(raw.get("default", {}))
        screens = {
            name: ScreenPolicy.from_dict(cfg, default)
            for name, cfg in (raw.get("screens") or {}).items()
        }
        options = DiffOptions()
        diff_raw = raw.get("diff") or {}
        if "ignore" in raw:
            diff_raw.setdefault("ignore", raw["ignore"])
        for key in ("tolerance", "block", "min_block_density", "min_region_px", "merge_radius"):
            if key in diff_raw:
                setattr(options, key, type(getattr(options, key))(diff_raw[key]))
        if "ignore" in diff_raw:
            options.ignore = list(diff_raw["ignore"])
        if "ignore_boxes" in diff_raw:
            options.ignore_boxes = [tuple(b) for b in diff_raw["ignore_boxes"]]
        return cls(
            default=default,
            screens=screens,
            compare_against=raw.get("compare_against", "approved"),
            diff_options=options,
        )

    def for_screen(self, name: str) -> ScreenPolicy:
        return self.screens.get(name, self.default)


@dataclass
class Alert:
    severity: str
    code: str
    screen: Screen
    build: Build
    message: str
    baseline: Optional[Build] = None
    regions: List[Dict[str, Any]] = field(default_factory=list)
    diff: Optional[DiffResult] = None

    @property
    def is_error(self) -> bool:
        return self.severity == ERROR

    def to_dict(self) -> Dict[str, Any]:
        return {
            "severity": self.severity,
            "code": self.code,
            "screen": self.screen.to_dict(),
            "build": self.build.to_dict(),
            "baseline": self.baseline.to_dict() if self.baseline else None,
            "message": self.message,
            "regions": self.regions,
        }


@dataclass
class AlertReport:
    app: str
    build: Optional[Build]
    alerts: List[Alert] = field(default_factory=list)
    screens_checked: int = 0

    @property
    def errors(self) -> List[Alert]:
        return [a for a in self.alerts if a.severity == ERROR]

    @property
    def warnings(self) -> List[Alert]:
        return [a for a in self.alerts if a.severity == WARNING]

    @property
    def ok(self) -> bool:
        return not self.errors

    def exit_code(self, fail_on: str = ERROR) -> int:
        limit = _RANK.get(fail_on, 0)
        return 1 if any(_RANK[a.severity] <= limit for a in self.alerts) else 0

    def sorted(self) -> List[Alert]:
        return sorted(self.alerts, key=lambda a: (_RANK[a.severity], a.screen.name))

    def to_dict(self) -> Dict[str, Any]:
        return {
            "app": self.app,
            "build": self.build.to_dict() if self.build else None,
            "screens_checked": self.screens_checked,
            "counts": {
                "error": len(self.errors),
                "warning": len(self.warnings),
                "info": len([a for a in self.alerts if a.severity == INFO]),
            },
            "alerts": [a.to_dict() for a in self.sorted()],
        }

    def to_markdown(self, title: str = "Atlas visual review") -> str:
        icons = {ERROR: "🔴", WARNING: "🟡", INFO: "⚪"}
        lines = ["## %s" % title, ""]
        build = self.build
        if build:
            head = "Build %s" % build.title
            if build.commit_url:
                head += " ([commit](%s))" % build.commit_url
            if build.pr_url:
                head += " ([PR #%d](%s))" % (build.pr_number, build.pr_url)
            lines += [head, ""]
        if not self.alerts:
            lines.append("No unexpected visual changes across %d screens." % self.screens_checked)
            return "\n".join(lines) + "\n"
        lines.append(
            "%d error, %d warning across %d screens."
            % (len(self.errors), len(self.warnings), self.screens_checked)
        )
        lines.append("")
        for alert in self.sorted():
            lines.append("- %s **%s** — %s" % (icons[alert.severity], alert.screen.name, alert.message))
            for region in alert.regions[:4]:
                lines.append("    - %s at %d,%d (%dx%d)" % (
                    region.get("description", "changed"),
                    region.get("x", 0), region.get("y", 0),
                    region.get("w", 0), region.get("h", 0),
                ))
        return "\n".join(lines) + "\n"


def check(
    client: AtlasClient,
    policy: Policy,
    store: Optional[ReviewStore] = None,
    build: Optional[Build] = None,
    baseline: Optional[Build] = None,
    screens: Optional[Sequence[Screen]] = None,
) -> AlertReport:
    """Evaluate one build against the policy. Returns every alert it trips."""
    store = store or ReviewStore()
    builds = client.builds()
    if not builds:
        return AlertReport(app=client.app, build=None)
    head = build or builds[-1]
    build_order = [b.id for b in builds if (b.uploaded_at or b.id) <= (head.uploaded_at or head.id)]
    by_id = {b.id: b for b in builds}
    report = AlertReport(app=client.app, build=head)

    head_versions = {v.screen.id: v for v in client.versions_at(head)}
    targets = list(screens) if screens else [v.screen for v in head_versions.values()]

    # A build nobody ever ran anything against has no screens to compare, and
    # "0 alerts" would read as "nothing regressed". Say what actually happened
    # instead, so an unmapped build can't pass for a clean one.
    if not any(v.has_image for v in head_versions.values()):
        report.alerts.append(Alert(
            severity=WARNING, code="build_not_mapped",
            screen=Screen(id="", name="(whole app)"), build=head,
            message="no Atlas data for this build -- run a test against it before trusting a clean review",
        ))
        return report

    for screen in targets:
        report.screens_checked += 1
        rules = policy.for_screen(screen.name)
        history = ScreenHistory(client, screen, builds, policy.diff_options)

        # Pick the frame this build is judged against.
        chosen: Optional[Build] = baseline
        if chosen is None and policy.compare_against == "approved":
            approved_id = store.baseline_build_id(screen.id, build_order[:-1])
            chosen = by_id.get(approved_id) if approved_id else None
        if chosen is None:
            earlier = [b for b in builds if b.id in build_order and b.id != head.id]
            for candidate in reversed(earlier):
                version = history.version(candidate)
                if version is not None and version.has_image:
                    chosen = candidate
                    break

        current = head_versions.get(screen.id)
        if current is None or not current.has_image:
            if rules.alert_on_removed and chosen is not None:
                report.alerts.append(Alert(
                    severity=WARNING, code="screen_missing", screen=screen, build=head,
                    baseline=chosen,
                    message="screen is not present in this build (last seen in %s)" % chosen.label,
                ))
            continue

        if chosen is None:
            if rules.alert_on_new:
                report.alerts.append(Alert(
                    severity=INFO, code="screen_new", screen=screen, build=head,
                    message="new screen, no earlier build to compare against",
                ))
            continue

        before = history.version(chosen)
        if before is None or not before.has_image:
            continue
        result = diff_versions(before, current, policy.diff_options, history.target_width())

        if not result.changed:
            continue

        # An explicit approval means a human already looked at this exact
        # build and accepted it, so its alerts drop to informational. Without
        # this, an intentional redesign keeps CI red forever.
        approved = store.status(screen.id, head.id) == APPROVED

        # 1. Frozen / watched regions -- the highest-signal rule.
        for watch in rules.watch:
            if not result.changed_in(watch.box):
                continue
            pct = 100.0 * result.changed_fraction_in(watch.box)
            if pct > watch.max_change_pct:
                severity = watch.severity
                note = ""
                if approved and not watch.ignore_approval:
                    severity, note = INFO, " (approved)"
                report.alerts.append(Alert(
                    severity=severity, code="watched_region_changed", screen=screen,
                    build=head, baseline=chosen, diff=result,
                    message='"%s" changed (%.1f%% of that region) vs %s%s'
                            % (watch.name, pct, chosen.label, note),
                    regions=[r.to_dict() for r in result.regions_in(watch.box)],
                ))

        # 2. Overall change budget.
        if result.percent > rules.max_change_pct:
            report.alerts.append(Alert(
                severity=INFO if approved else rules.severity,
                code="change_budget_exceeded", screen=screen,
                build=head, baseline=chosen, diff=result,
                message="%s vs %s (budget %.2f%%)%s" % (
                    result.summary, chosen.label, rules.max_change_pct,
                    " (approved)" if approved else ""),
                regions=[r.to_dict() for r in result.regions],
            ))

        # 3. Changed without a sign-off.
        if rules.require_approval and not approved:
            report.alerts.append(Alert(
                severity=ERROR, code="unapproved_change", screen=screen, build=head,
                baseline=chosen, diff=result,
                message="visual change is not approved (%s); approve with `atlas-review approve %s --build %s`"
                        % (result.summary, screen.name, head.label.split()[0]),
                regions=[r.to_dict() for r in result.regions],
            ))

        # 4. Frame geometry changed -- usually a device/orientation mismatch.
        if result.size_changed:
            report.alerts.append(Alert(
                severity=WARNING, code="frame_size_changed", screen=screen, build=head,
                baseline=chosen, diff=result,
                message="frame size differs from %s; diff may be unreliable" % chosen.label,
            ))

    return report


def post_webhook(report: AlertReport, url: str, timeout: int = 15) -> int:
    """POST the report as JSON. Returns the HTTP status; raises nothing fatal."""
    import urllib.error
    import urllib.request

    payload = json.dumps({
        "text": report.to_markdown(),
        "report": report.to_dict(),
    }).encode("utf-8")
    request = urllib.request.Request(
        url, data=payload,
        headers={"Content-Type": "application/json", "User-Agent": "atlas-review/0.1"},
    )
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            return int(response.status)
    except urllib.error.HTTPError as exc:
        return int(exc.code)
    except OSError:
        return 0
