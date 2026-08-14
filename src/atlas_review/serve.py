"""Local review server: the same report UI, but approvals write to the store.

Static reports are shareable; served reports are actionable. Both render from
the same payload, so a reviewer sees the same thing either way.
"""

from __future__ import annotations

import json
import webbrowser
from functools import partial
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import TYPE_CHECKING, Any, Dict

from .report import build_report
from .review import Comment, Decision

if TYPE_CHECKING:  # pragma: no cover
    from .api import AtlasReview


class _Handler(SimpleHTTPRequestHandler):
    review: "AtlasReview"

    def log_message(self, fmt: str, *args: Any) -> None:  # keep the console quiet
        return

    def _json(self, status: int, payload: Dict[str, Any]) -> None:
        body = json.dumps(payload).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_POST(self) -> None:  # noqa: N802 (stdlib naming)
        try:
            length = int(self.headers.get("Content-Length") or 0)
            payload = json.loads(self.rfile.read(length) or b"{}")
        except (ValueError, json.JSONDecodeError):
            return self._json(400, {"error": "bad request body"})

        store = self.review.store
        try:
            if self.path == "/api/decide":
                decision = Decision(
                    screen_id=payload["screen_id"],
                    build_id=payload["build_id"],
                    status=payload["status"],
                    author=payload.get("author") or "reviewer",
                    decided_at=payload.get("decided_at") or "",
                    note=payload.get("note", ""),
                )
                store.decisions[store._key(decision.screen_id, decision.build_id)] = decision
            elif self.path == "/api/comment":
                store.comments.append(Comment(
                    id=payload["id"],
                    screen_id=payload["screen_id"],
                    build_id=payload["build_id"],
                    body=payload["body"],
                    author=payload.get("author") or "reviewer",
                    created_at=payload.get("created_at") or "",
                    region=payload.get("region"),
                    resolved=bool(payload.get("resolved")),
                ))
            else:
                return self._json(404, {"error": "no such endpoint"})
        except KeyError as exc:
            return self._json(400, {"error": "missing field %s" % exc})
        store.save()
        self._json(200, {"ok": True, "saved_to": str(store.path)})


def serve(
    review: "AtlasReview",
    out: Path,
    host: str = "127.0.0.1",
    port: int = 7391,
    open_browser: bool = True,
) -> None:
    """Render the report into `out` and serve it until interrupted."""
    build_report(review, out=out, served=True)
    handler_cls = type("BoundHandler", (_Handler,), {"review": review})
    handler = partial(handler_cls, directory=str(out))
    server = ThreadingHTTPServer((host, port), handler)
    url = "http://%s:%d/index.html" % (host, port)
    print("atlas-review serving %s" % url)
    print("approvals and comments write to %s" % review.store.path)
    if open_browser:
        webbrowser.open(url)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nstopped")
    finally:
        server.server_close()
