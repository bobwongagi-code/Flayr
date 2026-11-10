"""Loopback-only browser fixture: static frontend, fake jobs, no task execution."""

from __future__ import annotations

import argparse
import json
import mimetypes
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlsplit

ROOT = Path(__file__).resolve().parents[1]
STATIC = {"/": "index.html"}
STATIC.update(
    {
        f"/{name}": name
        for name in (
            "index.html",
            "styles.css",
            "app.js",
            "components/audience-switch.js",
            "components/job-list.js",
            "components/report-view.js",
        )
    }
)
JOBS = [
    {
        "id": name,
        "name": label,
        "status": status,
        "workspace_id": "browser-fixture",
        "product_name": "Synthetic fixture",
        "market": "Test",
        "progress": 32,
        "failure_reason": "Synthetic failure" if status == "failed" else "",
        **(
            {
                "bd_report_url": f"/reports/{name}/internal.html",
                "creator_report_url": f"/reports/{name}/creator.html",
            }
            if status == "completed"
            else {}
        ),
    }
    for name, label, status in (
        ("ready", "01 Ready", "completed"),
        ("slow-head", "02 Slow HEAD", "completed"),
        ("slow-frame", "03 Slow iframe", "completed"),
        ("rejected", "04 Rejected", "completed"),
        ("running", "05 Running", "running"),
        ("failed", "06 Failed", "failed"),
    )
]


class FixtureServer(ThreadingHTTPServer):
    def __init__(self, port: int = 0):
        self.events: list[dict[str, object]] = []
        self.event_lock = threading.Lock()
        self.report_delay = 35.0
        super().__init__(("127.0.0.1", port), FixtureHandler)


class FixtureHandler(BaseHTTPRequestHandler):
    server: FixtureServer

    def log_message(self, *_args: object) -> None:
        pass

    def _send(self, status: int, payload: bytes, content_type: str) -> None:
        try:
            self.send_response(status)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(payload)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            if self.command != "HEAD":
                self.wfile.write(payload)
        except (BrokenPipeError, ConnectionResetError):
            pass  # Expected when a browser cancels a delayed synthetic response.

    def _json(self, value: object, status: int = 200) -> None:
        self._send(status, json.dumps(value).encode("utf-8"), "application/json")

    def do_POST(self) -> None:
        self._json({"error": "Fixture is read-only; no tasks can be submitted"}, 405)

    do_DELETE = do_POST
    do_PUT = do_POST

    def do_HEAD(self) -> None:
        self.do_GET()

    def do_GET(self) -> None:
        path = urlsplit(self.path).path
        with self.server.event_lock:
            self.server.events.append({"method": self.command, "path": path})
        if path in STATIC:
            source = ROOT / "frontend" / STATIC[path]
            content_type = (
                "text/javascript"
                if source.suffix == ".js"
                else (
                    mimetypes.guess_type(source.name)[0] or "application/octet-stream"
                )
            )
            self._send(200, source.read_bytes(), content_type + "; charset=utf-8")
        elif path == "/api/jobs":
            self._json({"jobs": JOBS})
        elif path.startswith("/api/jobs/"):
            job = next((job for job in JOBS if path == f"/api/jobs/{job['id']}"), None)
            self._json(job or {"error": "Fixture job not found"}, 200 if job else 404)
        elif path == "/fixture/events":
            with self.server.event_lock:
                events = list(self.server.events)
            self._json(events)
        elif path.startswith("/reports/"):
            parts = path.split("/")
            if (
                len(parts) != 4
                or parts[2] not in {job["id"] for job in JOBS[:4]}
                or parts[3] not in {"internal.html", "creator.html"}
            ):
                self._json({"error": "Unknown fixture report"}, 404)
                return
            job_id, audience = parts[2], parts[3]
            if audience == "creator.html":
                if job_id == "rejected":
                    self._json({"error": "Synthetic report rejection"}, 503)
                    return
                if (job_id == "slow-head" and self.command == "HEAD") or (
                    job_id == "slow-frame" and self.command == "GET"
                ):
                    time.sleep(self.server.report_delay)
            self._send(
                200,
                (
                    '<!doctype html><html lang="en"><meta charset="utf-8">'
                    '<body style="font:24px sans-serif;padding:24px;background:#fff">'
                    f"<h1>{job_id} / {audience}</h1><p>SYNTHETIC REPORT ONLY</p></body></html>"
                ).encode(),
                "text/html; charset=utf-8",
            )
        else:
            self._json({"error": "Not in fixture allowlist"}, 404)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--port", type=int, default=0)
    args = parser.parse_args()
    with FixtureServer(args.port) as server:
        print(f"http://127.0.0.1:{server.server_port}", flush=True)
        try:
            server.serve_forever()
        except KeyboardInterrupt:
            pass


if __name__ == "__main__":
    main()
