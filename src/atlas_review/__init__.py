"""atlas-review: visual review for Revyl Atlas screens.

Turns an app's Atlas screens into a reviewable build history -- image diffs
between builds, the commit/PR/author behind each one, which build first changed
a screen, approvals and comments on the screen itself, and alerts when
something changes that nobody signed off on.

    from atlas_review import AtlasReview

    rv = AtlasReview("4cf9100b-2d6d-4771-bbc2-213278f0864e")
    rv.diff("trips_list", "#3", "#4").render("overlay").save("diff.png")
    print(rv.blame("trips_list").summary)
    rv.approve("trips_list", "#5", note="new filter chips are intentional")
    rv.report("out/")
"""

from .alerts import Alert, AlertReport, Policy, ScreenPolicy, WatchRegion, check
from .api import AtlasReview
from .client import AtlasClient, RevylError
from .diff import DiffOptions, DiffResult, Region, compare_images, diff_versions
from .models import Build, Screen, ScreenVersion
from .review import APPROVED, PENDING, REJECTED, Comment, Decision, ReviewStore
from .timeline import Blame, ScreenHistory, history

__version__ = "0.1.0"

__all__ = [
    "APPROVED",
    "PENDING",
    "REJECTED",
    "Alert",
    "AlertReport",
    "AtlasClient",
    "AtlasReview",
    "Blame",
    "Build",
    "Comment",
    "Decision",
    "DiffOptions",
    "DiffResult",
    "Policy",
    "Region",
    "ReviewStore",
    "RevylError",
    "Screen",
    "ScreenHistory",
    "ScreenPolicy",
    "ScreenVersion",
    "WatchRegion",
    "check",
    "compare_images",
    "diff_versions",
    "history",
    "__version__",
]
