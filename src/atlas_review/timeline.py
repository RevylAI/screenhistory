"""Per-screen build history, and finding the first build where something changed.

Two ways to answer "when did this change?":

* `first_change()` scans adjacent builds oldest-first. Exact, and it also tells
  you every later change; costs one diff per build.
* `bisect()` binary-searches for the earliest build that differs from a
  baseline, optionally inside one region. O(log n) frames instead of O(n),
  which is what makes the question cheap on an app with hundreds of builds.

Bisect assumes the change persists once introduced (the usual case for "when
did this badge appear"). It verifies its answer against the immediately
preceding build, so a wrong monotonicity assumption surfaces instead of lying.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Callable, Dict, List, Optional, Sequence

from .client import AtlasClient
from .diff import Box, DiffOptions, DiffResult, diff_versions
from .models import Build, Screen, ScreenVersion

# Must match report.build_report's `max_width`: the report embeds frames at
# this cap, and the CLI has to measure the same pixels the browser does.
MAX_FRAME_WIDTH = 460


@dataclass
class Blame:
    """Where a visual change was introduced."""

    screen: Screen
    build: Build                       # first build showing the change
    previous: Optional[Build]          # last build without it
    diff: Optional[DiffResult]
    region: Optional[Box] = None
    method: str = "scan"
    builds_examined: int = 0
    verified: bool = True

    @property
    def summary(self) -> str:
        who = self.build.author or "unknown author"
        parts = ['%s first changed in %s' % (self.screen.name, self.build.title)]
        parts.append("by %s" % who)
        if self.diff is not None:
            parts.append(self.diff.summary)
        if self.previous is not None:
            parts.append("last unchanged build: %s" % self.previous.label)
        return " — ".join(parts)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "screen": self.screen.to_dict(),
            "build": self.build.to_dict(),
            "previous_build": self.previous.to_dict() if self.previous else None,
            "region": list(self.region) if self.region else None,
            "method": self.method,
            "builds_examined": self.builds_examined,
            "verified": self.verified,
            "diff": self.diff.to_dict() if self.diff else None,
            "summary": self.summary,
        }


class ScreenHistory:
    """One screen across an app's builds, loading frames only when asked."""

    def __init__(
        self,
        client: AtlasClient,
        screen: Screen,
        builds: Optional[Sequence[Build]] = None,
        options: Optional[DiffOptions] = None,
    ) -> None:
        self.client = client
        self.screen = screen
        self.builds: List[Build] = list(builds if builds is not None else client.builds())
        self.options = options or DiffOptions()
        self._cache: Dict[str, Optional[ScreenVersion]] = {}
        self._loads = 0
        self._target: Optional[int] = None

    # -- access -----------------------------------------------------------

    def version(self, build: Build) -> Optional[ScreenVersion]:
        """This screen at one build, or None if the screen isn't in that build."""
        if build.id in self._cache:
            return self._cache[build.id]
        self._loads += 1
        found: Optional[ScreenVersion] = None
        for candidate in self.client.versions_at(build):
            if candidate.screen.id == self.screen.id:
                found = candidate
                break
        self._cache[build.id] = found
        return found

    def versions(self) -> List[ScreenVersion]:
        """Every build where this screen appears, oldest first."""
        out: List[ScreenVersion] = []
        for build in self.builds:
            version = self.version(build)
            if version is not None and version.has_image:
                out.append(version)
        return out

    @property
    def frames_loaded(self) -> int:
        return self._loads

    def target_width(self, cap: int = MAX_FRAME_WIDTH) -> Optional[int]:
        """The one scale every frame of this screen is compared at.

        Without it, a pair's numbers depend on which pair it is -- comparing
        builds 2 and 3 would normalize to a different width than 1 and 3, and
        the report (which normalizes per screen) would disagree with the CLI.
        """
        if self._target is not None:
            return self._target
        from PIL import Image

        widths = []
        for version in self.versions():
            try:
                with Image.open(version.image_path) as probe:
                    widths.append(probe.size[0])
            except OSError:
                continue
        self._target = min(widths + [cap]) if widths else None
        return self._target

    # -- change detection -------------------------------------------------

    def changes(self, options: Optional[DiffOptions] = None) -> List[DiffResult]:
        """Diff every adjacent pair of builds containing this screen."""
        options = options or self.options
        versions = self.versions()
        out: List[DiffResult] = []
        for prev, curr in zip(versions, versions[1:]):
            out.append(diff_versions(prev, curr, options, self.target_width()))
        return out

    def first_change(
        self,
        threshold: float = 0.0,
        since: Optional[Build] = None,
        region: Optional[Box] = None,
        options: Optional[DiffOptions] = None,
    ) -> Optional[Blame]:
        """Earliest build whose frame differs from the one before it.

        `threshold` is a fraction of comparable pixels (0.001 == 0.1%).
        `region` restricts the question to one normalized box.
        """
        options = options or self.options
        versions = self.versions()
        if since is not None:
            start = next((i for i, v in enumerate(versions) if v.build.id == since.id), 0)
            versions = versions[start:]
        examined = 0
        for prev, curr in zip(versions, versions[1:]):
            result = diff_versions(prev, curr, options, self.target_width())
            examined += 1
            if _matches(result, threshold, region):
                return Blame(
                    screen=self.screen,
                    build=curr.build,
                    previous=prev.build,
                    diff=result,
                    region=region,
                    method="scan",
                    builds_examined=examined,
                )
        return None

    def bisect(
        self,
        region: Optional[Box] = None,
        threshold: float = 0.0,
        baseline: Optional[Build] = None,
        target: Optional[Build] = None,
        options: Optional[DiffOptions] = None,
    ) -> Optional[Blame]:
        """Binary-search for the first build that differs from `baseline`.

        Costs ~log2(n) frame loads instead of n. Use when the app has many
        builds and you only care where one change came from.
        """
        options = options or self.options
        versions = self.versions()
        if len(versions) < 2:
            return None
        lo_index = 0
        if baseline is not None:
            lo_index = next((i for i, v in enumerate(versions) if v.build.id == baseline.id), 0)
        hi_index = len(versions) - 1
        if target is not None:
            hi_index = next(
                (i for i, v in enumerate(versions) if v.build.id == target.id), len(versions) - 1
            )
        if hi_index <= lo_index:
            return None

        base = versions[lo_index]
        examined = 0

        def differs(index: int) -> bool:
            nonlocal examined
            examined += 1
            return _matches(
                diff_versions(base, versions[index], options, self.target_width()),
                threshold, region)

        if not differs(hi_index):
            return None  # nothing changed across the whole range

        lo, hi = lo_index, hi_index  # invariant: lo is clean, hi differs
        while hi - lo > 1:
            mid = (lo + hi) // 2
            if differs(mid):
                hi = mid
            else:
                lo = mid

        culprit, previous = versions[hi], versions[lo]
        result = diff_versions(previous, culprit, options, self.target_width())
        # Confirm the answer holds against its immediate predecessor.
        verified = _matches(result, threshold, region)
        return Blame(
            screen=self.screen,
            build=culprit.build,
            previous=previous.build,
            diff=result,
            region=region,
            method="bisect",
            builds_examined=examined + 1,
            verified=verified,
        )

    def timeline(self, options: Optional[DiffOptions] = None) -> List[Dict[str, Any]]:
        """Filmstrip data: one entry per build, each carrying its delta."""
        options = options or self.options
        versions = self.versions()
        out: List[Dict[str, Any]] = []
        previous: Optional[ScreenVersion] = None
        for version in versions:
            entry: Dict[str, Any] = {
                "build": version.build.to_dict(),
                "image_path": str(version.image_path) if version.image_path else None,
                "observation_count": version.observation_count,
                "diff": None,
            }
            if previous is not None:
                entry["diff"] = diff_versions(
                    previous, version, options, self.target_width()).to_dict()
            out.append(entry)
            previous = version
        return out


def _matches(result: DiffResult, threshold: float, region: Optional[Box]) -> bool:
    """Does a diff count as a change, given a threshold and optional region?"""
    if region is None:
        if result.size_changed and threshold <= 0:
            return True
        return result.changed and result.score > threshold
    # Measure inside the box itself: `threshold` is a fraction of the box area,
    # so "0.02" reads as "more than 2% of the price column changed".
    changed = result.changed_in(region)
    if not changed:
        return False
    return result.changed_fraction_in(region) > threshold


def history(
    client: AtlasClient,
    screen: Screen,
    options: Optional[DiffOptions] = None,
) -> ScreenHistory:
    return ScreenHistory(client, screen, client.builds(), options)
