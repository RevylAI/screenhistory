"""Core data types: builds, screens, and a screen's version at one build."""

from __future__ import annotations

import datetime as _dt
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional

_GITHUB_SSH = re.compile(r"git@([^:]+):(.+?)(?:\.git)?$")
_GITHUB_HTTPS = re.compile(r"https?://([^/]+)/(.+?)(?:\.git)?/?$")


def _parse_ts(value: Optional[str]) -> Optional[_dt.datetime]:
    """Parse the several timestamp shapes Revyl build metadata uses."""
    if not value:
        return None
    text = value.strip().replace("Z", "+00:00")
    try:
        return _dt.datetime.fromisoformat(text)
    except ValueError:
        pass
    for fmt in ("%Y-%m-%d %H:%M:%S %z", "%Y-%m-%dT%H:%M:%S%z", "%Y-%m-%d %H:%M:%S"):
        try:
            return _dt.datetime.strptime(text, fmt)
        except ValueError:
            continue
    return None


def _humanize(ts: Optional[_dt.datetime]) -> str:
    if ts is None:
        return "unknown time"
    if ts.tzinfo is None:
        ts = ts.replace(tzinfo=_dt.timezone.utc)
    delta = _dt.datetime.now(_dt.timezone.utc) - ts
    secs = int(delta.total_seconds())
    if secs < 0:
        return ts.strftime("%b %-d, %Y")
    for limit, div, unit in ((60, 1, "s"), (3600, 60, "m"), (86400, 3600, "h")):
        if secs < limit:
            return "just now" if secs < 5 else "%d%s ago" % (secs // div, unit)
    days = secs // 86400
    if days < 30:
        return "%dd ago" % days
    return ts.strftime("%b %-d, %Y")


@dataclass
class Build:
    """One uploaded app build, plus whatever git provenance came with it.

    Requirement: a build must be identifiable by a human. The raw Revyl
    identity is a UUID and a timestamped version string, neither of which a
    reviewer can tell apart at a glance, so every build carries a `label`.
    """

    id: str
    version: str
    uploaded_at: Optional[_dt.datetime] = None
    package_name: str = ""
    ordinal: int = 0  # 1-based position in the app's build history, oldest first
    commit: Optional[str] = None
    commit_short: Optional[str] = None
    branch: Optional[str] = None
    message: Optional[str] = None
    author: Optional[str] = None
    committed_at: Optional[_dt.datetime] = None
    dirty: bool = False
    remote: Optional[str] = None
    pr_number: Optional[int] = None
    raw: Dict[str, Any] = field(default_factory=dict)

    @classmethod
    def from_api(cls, payload: Dict[str, Any], ordinal: int = 0) -> "Build":
        meta = payload.get("metadata") or {}
        git = meta.get("git") or {}
        return cls(
            id=payload.get("id", ""),
            version=payload.get("version", ""),
            uploaded_at=_parse_ts(payload.get("uploaded_at")),
            package_name=payload.get("package_name", "") or meta.get("package_id", ""),
            ordinal=ordinal,
            commit=git.get("commit"),
            commit_short=git.get("commit_short") or (git.get("commit") or "")[:7] or None,
            branch=git.get("branch"),
            message=git.get("message"),
            author=git.get("author"),
            committed_at=_parse_ts(git.get("timestamp")),
            dirty=bool(git.get("dirty")),
            remote=git.get("remote"),
            pr_number=git.get("pr") or git.get("pr_number"),
            raw=payload,
        )

    @property
    def short_id(self) -> str:
        return self.id[:8]

    @property
    def when(self) -> Optional[_dt.datetime]:
        return self.committed_at or self.uploaded_at

    @property
    def subject(self) -> str:
        """First line of the commit message."""
        if not self.message:
            return ""
        return self.message.strip().splitlines()[0]

    @property
    def label(self) -> str:
        """Short, scannable name for filmstrips and tables: `#4 6149d68`."""
        head = "#%d" % self.ordinal if self.ordinal else self.short_id
        if self.commit_short:
            return "%s %s" % (head, self.commit_short)
        return "%s %s" % (head, self.version)

    @property
    def title(self) -> str:
        """Full one-line name: `#4 · main · 6149d68 · "search field" · 2h ago`."""
        parts: List[str] = ["#%d" % self.ordinal if self.ordinal else self.short_id]
        if self.branch:
            parts.append(self.branch)
        if self.commit_short:
            parts.append(self.commit_short + ("*" if self.dirty else ""))
        if self.subject:
            subject = self.subject
            if len(subject) > 48:
                subject = subject[:47] + "…"
            parts.append('"%s"' % subject)
        parts.append(_humanize(self.when))
        return " · ".join(parts)

    @property
    def relative_time(self) -> str:
        return _humanize(self.when)

    def _repo_slug(self) -> Optional[str]:
        """`(host, owner/repo)` parsed from the git remote, if there is one."""
        if not self.remote:
            return None
        for pattern in (_GITHUB_SSH, _GITHUB_HTTPS):
            m = pattern.match(self.remote.strip())
            if m:
                return (m.group(1), m.group(2))
        return None

    @property
    def commit_url(self) -> Optional[str]:
        slug = self._repo_slug()
        if not slug or not self.commit:
            return None
        return "https://%s/%s/commit/%s" % (slug[0], slug[1], self.commit)

    @property
    def pr_url(self) -> Optional[str]:
        slug = self._repo_slug()
        if not slug or not self.pr_number:
            return None
        return "https://%s/%s/pull/%d" % (slug[0], slug[1], self.pr_number)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "id": self.id,
            "ordinal": self.ordinal,
            "version": self.version,
            "label": self.label,
            "title": self.title,
            "branch": self.branch,
            "commit": self.commit,
            "commit_short": self.commit_short,
            "message": self.subject,
            "author": self.author,
            "dirty": self.dirty,
            "uploaded_at": self.uploaded_at.isoformat() if self.uploaded_at else None,
            "committed_at": self.committed_at.isoformat() if self.committed_at else None,
            "relative_time": self.relative_time,
            "commit_url": self.commit_url,
            "pr_url": self.pr_url,
            "pr_number": self.pr_number,
        }


@dataclass
class Screen:
    """A canonical Atlas screen node -- the identity that persists across builds."""

    id: str
    name: str
    product_area: str = ""
    screen_kind: str = ""
    description: str = ""

    @classmethod
    def from_node(cls, node: Dict[str, Any]) -> "Screen":
        return cls(
            id=node.get("id", ""),
            name=node.get("semantic_name") or node.get("label") or node.get("id", "")[:8],
            product_area=node.get("product_area", "") or "",
            screen_kind=node.get("screen_kind", "") or "",
            description=node.get("semantic_description", "") or "",
        )

    @property
    def short_id(self) -> str:
        return self.id[:8]

    def viewer_url(self, app_id: str) -> str:
        """Durable Atlas viewer deep link (graph `viewer_url` comes back null)."""
        return "https://app.revyl.ai/apps/%s/atlas?focus=screen&entityId=%s" % (app_id, self.id)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "id": self.id,
            "name": self.name,
            "product_area": self.product_area,
            "screen_kind": self.screen_kind,
            "description": self.description,
        }


@dataclass
class ScreenVersion:
    """One screen as it looked at one build: the frame plus where it came from."""

    screen: Screen
    build: Build
    image_path: Optional[Path] = None
    observation_count: int = 0
    screenshot_url: Optional[str] = None

    @property
    def has_image(self) -> bool:
        return self.image_path is not None and Path(self.image_path).exists()

    def to_dict(self) -> Dict[str, Any]:
        return {
            "screen": self.screen.to_dict(),
            "build": self.build.to_dict(),
            "image_path": str(self.image_path) if self.image_path else None,
            "observation_count": self.observation_count,
        }
