"""Thin wrapper over the `revyl` CLI for the two calls this SDK needs.

`revyl build list` gives the build history (and the git metadata that joins a
build to a commit/PR). `revyl atlas graph --build <uuid> --screenshots` gives
that build's screen nodes with a downloadable frame each. Everything else in
the SDK works off those two payloads.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
from pathlib import Path
from typing import Any, Dict, List, Optional

from .models import Build, Screen, ScreenVersion


class RevylError(RuntimeError):
    pass


def find_revyl() -> str:
    """Locate the revyl binary: $REVYL_BIN, then PATH, then the default install."""
    explicit = os.environ.get("REVYL_BIN")
    if explicit:
        return explicit
    found = shutil.which("revyl")
    if found:
        return found
    default = Path.home() / ".revyl" / "bin" / "revyl"
    if default.exists():
        return str(default)
    raise RevylError("revyl CLI not found; set REVYL_BIN or add revyl to PATH")


class AtlasClient:
    """Reads Atlas + build data for one app.

    Screenshots are cached under `cache_dir` keyed by build id, so re-running a
    diff or rebuilding a report costs nothing after the first pull. Presigned
    Atlas URLs expire in an hour; the cached PNG does not.
    """

    def __init__(
        self,
        app: str,
        cache_dir: Optional[Path] = None,
        revyl_bin: Optional[str] = None,
        timeout: int = 180,
        repo: Optional[str] = None,
        refresh: bool = False,
    ) -> None:
        self.app = app
        self.cache_dir = Path(cache_dir or Path(".atlas-review") / "cache")
        self.revyl_bin = revyl_bin or find_revyl()
        self.timeout = timeout
        # `owner/repo` or a git URL; overrides whatever repo we happen to be in.
        self.repo = repo
        self.refresh = refresh
        self._builds: Optional[List[Build]] = None
        self._graphs: Dict[str, Dict[str, Any]] = {}

    # -- plumbing ---------------------------------------------------------

    def _run_json(self, args: List[str]) -> Dict[str, Any]:
        cmd = [self.revyl_bin] + args
        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=self.timeout)
        if proc.returncode != 0:
            raise RevylError(
                "`%s` failed (exit %d): %s"
                % (" ".join(cmd), proc.returncode, (proc.stderr or proc.stdout).strip()[:500])
            )
        text = proc.stdout.strip()
        # The CLI occasionally prefixes human log lines before the JSON body.
        start = min((i for i in (text.find("{"), text.find("[")) if i != -1), default=-1)
        if start == -1:
            raise RevylError("no JSON in output of `%s`: %s" % (" ".join(cmd), text[:200]))
        try:
            return json.loads(text[start:])
        except json.JSONDecodeError as exc:
            raise RevylError("could not parse JSON from `%s`: %s" % (" ".join(cmd), exc))

    # -- builds -----------------------------------------------------------

    def builds(self, refresh: bool = False) -> List[Build]:
        """Build history, oldest first, with 1-based ordinals attached."""
        if self._builds is not None and not refresh:
            return self._builds
        payload = self._run_json(["build", "list", "--app", self.app, "--json"])
        versions = payload.get("versions") or []
        ordered = sorted(versions, key=lambda v: v.get("uploaded_at") or "")
        builds = [Build.from_api(v, ordinal=i) for i, v in enumerate(ordered, start=1)]
        remote = self.repo or _git_remote()
        if remote and "/" in remote and "://" not in remote and not remote.startswith("git@"):
            remote = "https://github.com/%s" % remote  # bare `owner/repo`
        for build in builds:
            if not build.remote:
                build.remote = remote
        self._builds = builds
        return builds

    def build(self, ref: str) -> Build:
        """Resolve a build by uuid, uuid prefix, `#3`, version string, or `latest`."""
        builds = self.builds()
        if not builds:
            raise RevylError("app %s has no builds" % self.app)
        ref = str(ref).strip()
        if ref in ("latest", "head", ""):
            return builds[-1]
        if ref in ("first", "oldest"):
            return builds[0]
        if ref.startswith("#") and ref[1:].isdigit():
            index = int(ref[1:])
            for build in builds:
                if build.ordinal == index:
                    return build
            raise RevylError("no build #%d (app has %d builds)" % (index, len(builds)))
        for build in builds:
            if build.id == ref or build.version == ref:
                return build
        matches = [
            b
            for b in builds
            if b.id.startswith(ref) or (b.commit_short and b.commit_short.startswith(ref))
            or (b.commit and b.commit.startswith(ref))
        ]
        if len(matches) == 1:
            return matches[0]
        if len(matches) > 1:
            raise RevylError("build ref %r is ambiguous: %s" % (ref, ", ".join(m.label for m in matches)))
        raise RevylError("no build matching %r" % ref)

    # -- atlas graph ------------------------------------------------------

    def graph(self, build_id: str, with_screenshots: bool = True) -> Dict[str, Any]:
        """The Atlas node/edge payload scoped to one build.

        Cached on disk: a shipped build's Atlas projection is effectively
        immutable, and re-running the CLI once per build makes every repeat
        command take tens of seconds for no new information. `refresh=True`
        (or `--refresh`) re-pulls.
        """
        key = "%s:%s" % (build_id, with_screenshots)
        if key in self._graphs:
            return self._graphs[key]
        shot_dir = self.cache_dir / build_id
        disk = shot_dir / "graph.json"
        # A metadata-only read is satisfied by the richer cached payload, so
        # listing screens costs nothing once the frames have been pulled once.
        if not with_screenshots:
            richer = self._graphs.get("%s:True" % build_id)
            if richer is not None:
                return richer
            if not self.refresh and disk.exists():
                try:
                    payload = json.loads(disk.read_text())
                except (json.JSONDecodeError, OSError):
                    payload = None
                if payload is not None:
                    self._graphs[key] = payload
                    return payload
        if not self.refresh and disk.exists():
            try:
                payload = json.loads(disk.read_text())
            except (json.JSONDecodeError, OSError):
                payload = None
            if payload is not None and self._images_present(payload, with_screenshots):
                self._graphs[key] = payload
                return payload
        args = ["atlas", "graph", "--app", self.app, "--build", build_id, "--json"]
        if with_screenshots:
            shot_dir.mkdir(parents=True, exist_ok=True)
            args += ["--screenshots", "--screenshot-dir", str(shot_dir)]
        payload = self._run_json(args)
        if with_screenshots:
            try:
                shot_dir.mkdir(parents=True, exist_ok=True)
                disk.write_text(json.dumps(payload))
            except OSError:
                pass
        self._graphs[key] = payload
        return payload

    @staticmethod
    def _images_present(payload: Dict[str, Any], with_screenshots: bool) -> bool:
        """A cached graph is only usable while its downloaded frames survive."""
        if not with_screenshots:
            return True
        nodes = payload.get("nodes") or []
        if not nodes:
            return True
        for node in nodes:
            path = node.get("local_screenshot_path")
            if path and not Path(path).exists():
                return False
        return True

    def versions_at(self, build: Build, with_screenshots: bool = True) -> List[ScreenVersion]:
        """Every screen present in one build, as ScreenVersion records."""
        payload = self.graph(build.id, with_screenshots=with_screenshots)
        out: List[ScreenVersion] = []
        for node in payload.get("nodes") or []:
            path = node.get("local_screenshot_path")
            image = Path(path) if path else None
            if image is not None and not image.is_absolute():
                image = Path.cwd() / image
            out.append(
                ScreenVersion(
                    screen=Screen.from_node(node),
                    build=build,
                    image_path=image if (image and image.exists()) else None,
                    observation_count=int(node.get("observation_count") or 0),
                    screenshot_url=node.get("screenshot_url"),
                )
            )
        return out

    def screens(self, build: Optional[Build] = None) -> List[Screen]:
        """Canonical screens. Without a build, the union across all builds."""
        if build is not None:
            return [v.screen for v in self.versions_at(build, with_screenshots=False)]
        seen: Dict[str, Screen] = {}
        for b in self.builds():
            for version in self.versions_at(b, with_screenshots=False):
                seen.setdefault(version.screen.id, version.screen)
        return list(seen.values())

    def resolve_screen(self, ref: str) -> Screen:
        """Resolve a screen by id, id prefix, or semantic name."""
        screens = self.screens()
        ref = str(ref).strip()
        for screen in screens:
            if screen.id == ref or screen.name == ref:
                return screen
        matches = [s for s in screens if s.id.startswith(ref) or s.name.startswith(ref)]
        if len(matches) == 1:
            return matches[0]
        if len(matches) > 1:
            raise RevylError("screen ref %r is ambiguous: %s" % (ref, ", ".join(s.name for s in matches)))
        known = ", ".join(s.name for s in screens) or "none"
        raise RevylError("no screen matching %r (known: %s)" % (ref, known))


def _git_remote(cwd: Optional[Path] = None) -> Optional[str]:
    """Origin URL of the repo we're running in -- used to build commit links."""
    try:
        proc = subprocess.run(
            ["git", "config", "--get", "remote.origin.url"],
            capture_output=True,
            text=True,
            timeout=10,
            cwd=str(cwd) if cwd else None,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    url = proc.stdout.strip()
    return url or None


def resolve_pr_numbers(builds: List[Build], repo: Optional[str] = None) -> None:
    """Fill in `pr_number` for each build by asking `gh` which PR held the commit.

    Best-effort: silently leaves builds alone when gh is missing or unauthed.
    """
    if not shutil.which("gh"):
        return
    for build in builds:
        if build.pr_number or not build.commit:
            continue
        args = [
            "gh", "api",
            "repos/{owner}/{repo}/commits/%s/pulls" % build.commit,
            "--jq", ".[0].number",
        ]
        if repo:
            args += ["-R", repo]
        try:
            proc = subprocess.run(args, capture_output=True, text=True, timeout=20)
        except (OSError, subprocess.SubprocessError):
            return
        value = proc.stdout.strip()
        if proc.returncode == 0 and value.isdigit():
            build.pr_number = int(value)
