"""Approvals and comments, stored next to the repo so review state is reviewable.

The store is a single JSON file (`.atlas-review/review.json`) meant to be
committed. That gives two things a hosted-only tool doesn't: the approval of a
screen travels with the branch that changed it, and "what was the last approved
look of this screen" is answerable offline, which is what every later diff is
measured against.
"""

from __future__ import annotations

import datetime as _dt
import json
import os
import uuid
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence

from .diff import Box

APPROVED = "approved"
REJECTED = "rejected"
PENDING = "pending"
VALID_STATUSES = (APPROVED, REJECTED, PENDING)


def _now() -> str:
    return _dt.datetime.now(_dt.timezone.utc).replace(microsecond=0).isoformat()


def _default_author() -> str:
    return os.environ.get("ATLAS_REVIEW_USER") or os.environ.get("USER") or "unknown"


@dataclass
class Comment:
    """A note on a screen at a build, optionally pinned to a region."""

    id: str
    screen_id: str
    build_id: str
    body: str
    author: str
    created_at: str
    region: Optional[List[float]] = None  # normalized [x, y, w, h]
    resolved: bool = False

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)


@dataclass
class Decision:
    """An approve/reject on a screen at a build."""

    screen_id: str
    build_id: str
    status: str
    author: str
    decided_at: str
    note: str = ""

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)


class ReviewStore:
    """JSON-backed approvals + comments. Safe to commit, safe to merge by hand."""

    VERSION = 1

    def __init__(self, path: Optional[Path] = None) -> None:
        self.path = Path(path or Path(".atlas-review") / "review.json")
        self.decisions: Dict[str, Decision] = {}
        self.comments: List[Comment] = []
        self.load()

    # -- persistence ------------------------------------------------------

    @staticmethod
    def _key(screen_id: str, build_id: str) -> str:
        return "%s@%s" % (screen_id, build_id)

    def load(self) -> "ReviewStore":
        if not self.path.exists():
            return self
        try:
            payload = json.loads(self.path.read_text())
        except (json.JSONDecodeError, OSError):
            return self
        for raw in payload.get("decisions", []):
            decision = Decision(**raw)
            self.decisions[self._key(decision.screen_id, decision.build_id)] = decision
        for raw in payload.get("comments", []):
            self.comments.append(Comment(**raw))
        return self

    def save(self) -> Path:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        payload = {
            "version": self.VERSION,
            "decisions": [d.to_dict() for d in self.decisions.values()],
            "comments": [c.to_dict() for c in self.comments],
        }
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n")
        tmp.replace(self.path)
        return self.path

    # -- decisions --------------------------------------------------------

    def decide(
        self,
        screen_id: str,
        build_id: str,
        status: str,
        author: Optional[str] = None,
        note: str = "",
    ) -> Decision:
        if status not in VALID_STATUSES:
            raise ValueError("status must be one of %s" % (VALID_STATUSES,))
        decision = Decision(
            screen_id=screen_id,
            build_id=build_id,
            status=status,
            author=author or _default_author(),
            decided_at=_now(),
            note=note,
        )
        self.decisions[self._key(screen_id, build_id)] = decision
        return decision

    def approve(self, screen_id: str, build_id: str, **kwargs: Any) -> Decision:
        return self.decide(screen_id, build_id, APPROVED, **kwargs)

    def reject(self, screen_id: str, build_id: str, **kwargs: Any) -> Decision:
        return self.decide(screen_id, build_id, REJECTED, **kwargs)

    def status(self, screen_id: str, build_id: str) -> str:
        decision = self.decisions.get(self._key(screen_id, build_id))
        return decision.status if decision else PENDING

    def decision(self, screen_id: str, build_id: str) -> Optional[Decision]:
        return self.decisions.get(self._key(screen_id, build_id))

    def baseline_build_id(self, screen_id: str, build_order: Sequence[str]) -> Optional[str]:
        """Most recent approved build for a screen, given builds oldest-first.

        This is what "unexpected change" is measured against: the last look a
        human signed off on, not merely the previous build.
        """
        for build_id in reversed(list(build_order)):
            if self.status(screen_id, build_id) == APPROVED:
                return build_id
        return None

    # -- comments ---------------------------------------------------------

    def comment(
        self,
        screen_id: str,
        build_id: str,
        body: str,
        author: Optional[str] = None,
        region: Optional[Box] = None,
    ) -> Comment:
        entry = Comment(
            id=uuid.uuid4().hex[:12],
            screen_id=screen_id,
            build_id=build_id,
            body=body,
            author=author or _default_author(),
            created_at=_now(),
            region=[float(v) for v in region] if region else None,
        )
        self.comments.append(entry)
        return entry

    def comments_for(
        self,
        screen_id: str,
        build_id: Optional[str] = None,
        include_resolved: bool = True,
    ) -> List[Comment]:
        out = [c for c in self.comments if c.screen_id == screen_id]
        if build_id is not None:
            out = [c for c in out if c.build_id == build_id]
        if not include_resolved:
            out = [c for c in out if not c.resolved]
        return sorted(out, key=lambda c: c.created_at)

    def resolve(self, comment_id: str, resolved: bool = True) -> Optional[Comment]:
        for entry in self.comments:
            if entry.id == comment_id:
                entry.resolved = resolved
                return entry
        return None

    def open_comment_count(self, screen_id: str, build_id: Optional[str] = None) -> int:
        return len(self.comments_for(screen_id, build_id, include_resolved=False))

    # -- interop ----------------------------------------------------------

    def merge(self, other: Dict[str, Any]) -> int:
        """Fold in a review payload exported by the HTML report. Returns count."""
        applied = 0
        for raw in other.get("decisions", []):
            try:
                decision = Decision(**raw)
            except TypeError:
                continue
            key = self._key(decision.screen_id, decision.build_id)
            existing = self.decisions.get(key)
            if existing is None or decision.decided_at >= existing.decided_at:
                self.decisions[key] = decision
                applied += 1
        known = {c.id for c in self.comments}
        for raw in other.get("comments", []):
            try:
                entry = Comment(**raw)
            except TypeError:
                continue
            if entry.id not in known:
                self.comments.append(entry)
                known.add(entry.id)
                applied += 1
        return applied

    def to_dict(self) -> Dict[str, Any]:
        return {
            "version": self.VERSION,
            "decisions": [d.to_dict() for d in self.decisions.values()],
            "comments": [c.to_dict() for c in self.comments],
        }
