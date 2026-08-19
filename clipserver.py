#!/usr/bin/env python3
"""Resolve stable clip links to short-lived S3 presigned URLs.

The reconciler writes https://<host>/clip/<event_id> into Notion. That link never
expires. This server turns it into a freshly signed S3 URL at click time, so the
signature only has to live for a few minutes.

Bind to loopback and put it behind `tailscale serve`: the tailnet is the access
control, and the Notion link is worthless to anyone not on it.
"""
from __future__ import annotations

import html
import logging
import os
import re
import sqlite3
import sys
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import unquote

from reconciler import Settings, s3_client

LOG = logging.getLogger("clipserver")

# Frigate event ids look like "1786809272.368455-x1xezx". Anything else is not an
# event, so it never reaches the database.
EVENT_ID = re.compile(r"^[0-9]+(?:\.[0-9]+)?-[A-Za-z0-9]+$")


def open_state_ro(path) -> sqlite3.Connection:
    conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True, timeout=10)
    conn.row_factory = sqlite3.Row
    return conn


def segment_keys(settings: Settings, event_id: str) -> list[str]:
    conn = open_state_ro(settings.state_db)
    try:
        rows = conn.execute(
            "SELECT s3_key FROM segment_delivery WHERE event_id=? AND uploaded_at IS NOT NULL ORDER BY s3_key",
            (event_id,),
        ).fetchall()
    finally:
        conn.close()
    return [r["s3_key"] for r in rows]


def manifest_key(settings: Settings, event_id: str) -> str | None:
    conn = open_state_ro(settings.state_db)
    try:
        row = conn.execute(
            "SELECT manifest_key FROM event_delivery WHERE event_id=? AND completed_at IS NOT NULL",
            (event_id,),
        ).fetchone()
    finally:
        conn.close()
    return row["manifest_key"] if row else None


def presign(client, settings: Settings, key: str) -> str:
    return client.generate_presigned_url(
        "get_object",
        Params={"Bucket": settings.bucket, "Key": key},
        ExpiresIn=settings.clip_url_ttl,
    )


PAGE = """<!doctype html><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>{event_id}</title>
<style>
 body{{font:15px/1.5 -apple-system,system-ui,sans-serif;margin:0;padding:1.5rem;
      background:#10161a;color:#e4eaed}}
 h1{{font-size:1rem;font-weight:600;margin:0 0 1.2rem;font-family:ui-monospace,Menlo,monospace;
     letter-spacing:-.02em;word-break:break-all}}
 .seg{{margin-bottom:1.5rem}}
 .lbl{{font:600 .7rem/1 ui-monospace,Menlo,monospace;letter-spacing:.1em;text-transform:uppercase;
       color:#7a8b93;margin-bottom:.4rem}}
 video{{width:100%;max-width:56rem;border-radius:3px;background:#000;display:block}}
 .empty{{color:#7a8b93}}
</style>
<h1>{event_id}</h1>
{body}
"""


class Handler(BaseHTTPRequestHandler):
    server_version = "clipserver"
    settings: Settings
    client = None

    def log_message(self, fmt, *args):  # never log query strings — they carry signatures
        LOG.info("%s %s", self.command, self.path.split("?", 1)[0])

    def _send(self, code: int, body: bytes = b"", ctype: str = "text/plain; charset=utf-8") -> None:
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Referrer-Policy", "no-referrer")
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(body)

    def _redirect(self, url: str) -> None:
        self.send_response(302)
        self.send_header("Location", url)
        self.send_header("Referrer-Policy", "no-referrer")
        self.send_header("Cache-Control", "no-store")
        self.send_header("Content-Length", "0")
        self.end_headers()

    def do_HEAD(self) -> None:
        self.do_GET()

    def do_GET(self) -> None:
        parts = [unquote(p) for p in self.path.split("?", 1)[0].strip("/").split("/") if p]

        if parts == ["healthz"]:
            return self._send(200, b"ok\n")

        if not parts or parts[0] not in {"clip", "manifest"} or not EVENT_ID.match(parts[1] if len(parts) > 1 else ""):
            return self._send(404, b"not found\n")

        kind, event_id = parts[0], parts[1]

        if kind == "manifest":
            key = manifest_key(self.settings, event_id)
            if not key:
                return self._send(404, b"no manifest for that event\n")
            return self._redirect(presign(self.client, self.settings, key))

        keys = segment_keys(self.settings, event_id)
        if not keys:
            return self._send(404, b"no delivered segments for that event\n")

        if len(parts) == 3:                       # /clip/<id>/<n> -> the signed URL itself
            if not parts[2].isdigit() or int(parts[2]) >= len(keys):
                return self._send(404, b"no such segment\n")
            return self._redirect(presign(self.client, self.settings, keys[int(parts[2])]))

        if len(parts) != 2:
            return self._send(404, b"not found\n")

        segs = "\n".join(
            '<div class="seg"><div class="lbl">{n}</div>'
            '<video controls preload="metadata" src="/clip/{eid}/{i}"></video></div>'.format(
                n=html.escape(k.rsplit("/", 1)[-1]), eid=html.escape(event_id), i=i)
            for i, k in enumerate(keys)
        )
        page = PAGE.format(event_id=html.escape(event_id), body=segs)
        return self._send(200, page.encode(), "text/html; charset=utf-8")


def main() -> int:
    logging.basicConfig(level=os.getenv("LOG_LEVEL", "INFO").upper(),
                        format="%(asctime)s %(levelname)s %(message)s")
    settings = Settings.from_env()
    if not settings.bucket:
        LOG.error("S3_BUCKET is required")
        return 2
    Handler.settings = settings
    Handler.client = s3_client(settings)
    port = int(os.getenv("CLIP_SERVER_PORT", "8787"))
    # Loopback only. `tailscale serve` fronts this; binding 0.0.0.0 would put private
    # footage on the LAN and defeat the point.
    server = ThreadingHTTPServer(("127.0.0.1", port), Handler)
    LOG.info("clipserver on http://127.0.0.1:%d (ttl %ds)", port, settings.clip_url_ttl)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        return 130
    finally:
        server.server_close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
