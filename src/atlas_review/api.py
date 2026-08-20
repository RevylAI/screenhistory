"""The one object most callers need: `AtlasReview`."""

from __future__ import annotations

from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence, Union

from .alerts import AlertReport, Policy, check
from .client import AtlasClient, resolve_pr_numbers
from .diff import Box, DiffOptions, DiffResult, diff_versions
from .models import Build, Screen, ScreenVersion, builds_through
from .review import Comment, Decision, ReviewStore
from .timeline import Blame, ScreenHistory

BuildRef = Union[str, Build, None]
ScreenRef = Union[str, Screen]


class AtlasReview:
    """Visual review over an app's Atlas screens.

    >>> rv = AtlasReview("4cf9100b-2d6d-4771-bbc2-213278f0864e")
    >>> rv.diff("trips_list", "#3", "#4").render("overlay").save("d.png")
    >>> rv.blame("trips_list").summary
    """

    def __init__(
        self,
        app: str,
        workdir: Optional[Path] = None,
        policy: Optional[Policy] = None,
        options: Optional[DiffOptions] = None,
        repo: Optional[str] = None,
        resolve_prs: bool = False,
        refresh: bool = False,
    ) -> None:
        self.workdir = Path(workdir or ".atlas-review")
        self.client = AtlasClient(app, cache_dir=self.workdir / "cache", repo=repo, refresh=refresh)
        self.store = ReviewStore(self.workdir / "review.json")
        self.policy = policy or Policy()
        self.options = options or self.policy.diff_options
        self._resolve_prs = resolve_prs
        self._histories: Dict[str, ScreenHistory] = {}

    @property
    def app(self) -> str:
        return self.client.app

    # -- reads ------------------------------------------------------------

    def builds(self) -> List[Build]:
        builds = self.client.builds()
        if self._resolve_prs:
            resolve_pr_numbers(builds, repo=self.client.repo)
            self._resolve_prs = False
        return builds

    def build(self, ref: BuildRef) -> Build:
        if isinstance(ref, Build):
            return ref
        self.builds()
        return self.client.build(ref if ref is not None else "latest")

    def screens(self, build: BuildRef = None) -> List[Screen]:
        return self.client.screens(self.build(build) if build is not None else None)

    def screen(self, ref: ScreenRef) -> Screen:
        return ref if isinstance(ref, Screen) else self.client.resolve_screen(ref)

    def history(self, screen: ScreenRef) -> ScreenHistory:
        resolved = self.screen(screen)
        if resolved.id not in self._histories:
            self._histories[resolved.id] = ScreenHistory(
                self.client, resolved, self.builds(), self.options
            )
        return self._histories[resolved.id]

    def version(self, screen: ScreenRef, build: BuildRef = None) -> Optional[ScreenVersion]:
        return self.history(screen).version(self.build(build))

    # -- diffing ----------------------------------------------------------

    def diff(
        self,
        screen: ScreenRef,
        before: BuildRef = None,
        after: BuildRef = None,
        options: Optional[DiffOptions] = None,
    ) -> DiffResult:
        """Compare a screen between two builds (defaults: previous vs latest)."""
        hist = self.history(screen)
        versions = hist.versions()
        if len(versions) < 2 and (before is None or after is None):
            raise ValueError(
                "%s appears in %d build(s); need two to diff" % (hist.screen.name, len(versions))
            )
        after_v = hist.version(self.build(after)) if after is not None else versions[-1]
        if after_v is None:
            raise ValueError("screen %s is missing from that build" % hist.screen.name)
        if before is None:
            index = next((i for i, v in enumerate(versions) if v.build.id == after_v.build.id), len(versions) - 1)
            # versions[max(0, index - 1)] would hand back after_v itself here,
            # and a frame diffed against itself reports a confident "no change".
            if index == 0:
                raise ValueError(
                    "%s first appears in %s -- no earlier build to compare it against"
                    % (hist.screen.name, after_v.build.label)
                )
            before_v = versions[index - 1]
        else:
            before_v = hist.version(self.build(before))
        if before_v is None:
            raise ValueError("screen %s is missing from one of those builds" % hist.screen.name)
        return diff_versions(before_v, after_v, options or self.options, hist.target_width())

    def screen_changes(self, screen: ScreenRef) -> List[DiffResult]:
        """Adjacent-build diffs for one screen, oldest first.

        The across-builds view of a single screen; `changes()` is the
        across-screens view of a single build.
        """
        return self.history(screen).changes(self.options)

    def blame(
        self,
        screen: ScreenRef,
        region: Optional[Box] = None,
        threshold: float = 0.0,
        method: str = "scan",
    ) -> Optional[Blame]:
        """Find the first build where this screen (or region) changed."""
        hist = self.history(screen)
        if method == "bisect":
            return hist.bisect(region=region, threshold=threshold, options=self.options)
        return hist.first_change(threshold=threshold, region=region, options=self.options)

    # -- review -----------------------------------------------------------

    def approve(
        self, screen: ScreenRef, build: BuildRef = None, note: str = "", author: Optional[str] = None
    ) -> Decision:
        decision = self.store.approve(
            self.screen(screen).id, self.build(build).id, note=note, author=author
        )
        self.store.save()
        return decision

    def reject(
        self, screen: ScreenRef, build: BuildRef = None, note: str = "", author: Optional[str] = None
    ) -> Decision:
        decision = self.store.reject(
            self.screen(screen).id, self.build(build).id, note=note, author=author
        )
        self.store.save()
        return decision

    def comment(
        self,
        screen: ScreenRef,
        body: str,
        build: BuildRef = None,
        region: Optional[Box] = None,
        author: Optional[str] = None,
    ) -> Comment:
        entry = self.store.comment(
            self.screen(screen).id, self.build(build).id, body, author=author, region=region
        )
        self.store.save()
        return entry

    def status(self, screen: ScreenRef, build: BuildRef = None) -> str:
        return self.store.status(self.screen(screen).id, self.build(build).id)

    def baseline(self, screen: ScreenRef) -> Optional[Build]:
        """Last build whose look was approved for this screen."""
        builds = self.builds()
        approved = self.store.baseline_build_id(self.screen(screen).id, [b.id for b in builds])
        return next((b for b in builds if b.id == approved), None)

    # -- alerts + report --------------------------------------------------

    def check(self, build: BuildRef = None, baseline: BuildRef = None) -> AlertReport:
        return check(
            self.client,
            self.policy,
            store=self.store,
            build=self.build(build),
            baseline=self.build(baseline) if baseline is not None else None,
        )

    def changes(
        self,
        build: BuildRef = None,
        baseline: BuildRef = None,
        screens: Optional[Sequence[ScreenRef]] = None,
    ) -> List[DiffResult]:
        """Every screen that changed in one build, biggest first.

        The cross-screen question: "what does this build actually look like
        different in?" On an app with one screen that is the same as `diff`; on
        a real one it is the only view that scales.
        """
        head = self.build(build)
        targets = [self.screen(s) for s in screens] if screens else self.screens(head)
        results: List[DiffResult] = []
        self.last_changes_mapped = False
        for screen in targets:
            hist = self.history(screen)
            current = hist.version(head)
            if current is None or not current.has_image:
                continue
            self.last_changes_mapped = True
            if baseline is not None:
                previous = hist.version(self.build(baseline))
            else:
                previous = self._previous_version(hist, head)
            if previous is None or not previous.has_image:
                continue
            result = diff_versions(previous, current, self.options, hist.target_width())
            if result.changed:
                results.append(result)
        results.sort(key=lambda r: r.score, reverse=True)
        return results

    def _previous_version(self, hist: ScreenHistory, head: Build):
        """The approved baseline for a screen, else the build before `head`."""
        builds = self.builds()
        order = [b for b in builds_through(builds, head) if b.id != head.id]
        if self.policy.compare_against == "approved":
            approved = self.store.baseline_build_id(hist.screen.id, [b.id for b in order])
            if approved:
                version = hist.version(next(b for b in builds if b.id == approved))
                if version is not None and version.has_image:
                    return version
        for candidate in reversed(order):
            version = hist.version(candidate)
            if version is not None and version.has_image:
                return version
        return None

    def report(
        self,
        out: Union[str, Path] = "atlas-review-report",
        screens: Optional[Sequence[ScreenRef]] = None,
        inline: bool = True,
        open_browser: bool = False,
        builds_limit: Optional[int] = None,
        log: Optional[Any] = None,
    ) -> Path:
        from .report import build_report

        return build_report(
            self,
            out=Path(out),
            screens=[self.screen(s) for s in screens] if screens else None,
            inline=inline,
            open_browser=open_browser,
            builds_limit=builds_limit,
            log=log,
        )
