#!/usr/bin/env python3
from __future__ import annotations

import argparse
import hashlib
import html
import json
import logging
import mimetypes
import os
import sqlite3
import tempfile
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable

import boto3
import requests
from botocore.config import Config as BotoConfig
from dotenv import load_dotenv

LOG = logging.getLogger("fregata-reconciler")


@dataclass(frozen=True)
class Settings:
    source_db: Path
    recordings_dir: Path
    state_db: Path
    bucket: str
    prefix: str
    region: str
    endpoint_url: str | None
    camera: str | None
    label: str
    pre_roll: float
    post_roll: float
    poll_seconds: float
    settle_seconds: float
    dry_run: bool
    upload_manifest: bool
    notion_token: str | None
    notion_database_id: str | None
    notion_version: str
    notion_include_person: bool
    notion_max_attempts: int
    clip_links: bool
    clip_url_ttl: int
    clip_refresh_seconds: int
    clip_aws_access_key_id: str | None
    clip_aws_secret_access_key: str | None

    @staticmethod
    def from_env() -> "Settings":
        load_dotenv()
        def b(name: str, default: bool) -> bool:
            return os.getenv(name, str(default)).strip().lower() in {"1", "true", "yes", "on"}
        camera = os.getenv("CAMERA", "").strip() or None
        clip_links = b("CLIP_LINKS", False)
        # 604800s (7 days) is the hard SigV4 ceiling; a larger value would presign URLs
        # that S3 rejects outright, so clamp instead of failing later at click time.
        clip_url_ttl = min(int(os.getenv("CLIP_URL_TTL_SECONDS", "604800")), 604_800)
        clip_refresh_seconds = int(os.getenv("CLIP_REFRESH_SECONDS", "432000"))
        if clip_links and clip_refresh_seconds >= clip_url_ttl:
            raise ValueError(
                "CLIP_REFRESH_SECONDS must be smaller than CLIP_URL_TTL_SECONDS: links are "
                "re-signed only once they are older than the refresh age, so a refresh age "
                "at or past the TTL guarantees every link in Notion dies before renewal")
        return Settings(
            source_db=Path(os.path.expanduser(os.getenv("FREGATA_DB_PATH", "~/Fregata/config/frigate.db"))),
            recordings_dir=Path(os.path.expanduser(os.getenv("FREGATA_RECORDINGS_DIR", "~/Fregata/media/recordings"))),
            state_db=Path(os.path.expanduser(os.getenv("STATE_DB_PATH", "./reconciler-state.db"))),
            bucket=os.getenv("S3_BUCKET", "").strip(),
            prefix=os.getenv("S3_PREFIX", "fregata").strip("/"),
            region=os.getenv("AWS_REGION", "us-east-1"),
            endpoint_url=os.getenv("S3_ENDPOINT_URL") or None,
            camera=camera,
            label=os.getenv("LABEL", "person"),
            pre_roll=float(os.getenv("PRE_ROLL_SECONDS", "10")),
            post_roll=float(os.getenv("POST_ROLL_SECONDS", "15")),
            poll_seconds=float(os.getenv("POLL_SECONDS", "30")),
            settle_seconds=float(os.getenv("SETTLE_SECONDS", "5")),
            dry_run=b("DRY_RUN", True),
            # Off by default: the manifest embeds the raw event row, sub_label included —
            # a real person's name when face recognition is on — ungated by
            # NOTION_INCLUDE_PERSON. An absent var must fail toward not publishing it.
            upload_manifest=b("UPLOAD_EVENT_MANIFEST", False),
            notion_token=os.getenv("NOTION_TOKEN", "").strip() or None,
            notion_database_id=os.getenv("NOTION_DATABASE_ID", "").strip() or None,
            notion_version=os.getenv("NOTION_VERSION", "2026-03-11").strip(),
            notion_include_person=b("NOTION_INCLUDE_PERSON", False),
            notion_max_attempts=int(os.getenv("NOTION_MAX_ATTEMPTS", "5")),
            clip_links=clip_links,
            clip_url_ttl=clip_url_ttl,
            clip_refresh_seconds=clip_refresh_seconds,
            clip_aws_access_key_id=os.getenv("CLIP_AWS_ACCESS_KEY_ID", "").strip() or None,
            clip_aws_secret_access_key=os.getenv("CLIP_AWS_SECRET_ACCESS_KEY", "").strip() or None,
        )


EVENT_REQUIRED = {"id", "camera", "label", "start_time", "end_time"}
RECORDING_REQUIRED = {"camera", "path", "start_time", "end_time"}


def utc_iso(ts: float | None) -> str | None:
    if ts is None:
        return None
    return datetime.fromtimestamp(float(ts), timezone.utc).isoformat()


def qident(name: str) -> str:
    return '"' + name.replace('"', '""') + '"'


def table_columns(conn: sqlite3.Connection, table: str) -> set[str]:
    return {row[1] for row in conn.execute(f"PRAGMA table_info({qident(table)})")}


def resolve_table(conn: sqlite3.Connection, candidates: Iterable[str], required: set[str]) -> tuple[str, set[str]]:
    tables = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    for table in candidates:
        if table in tables:
            cols = table_columns(conn, table)
            if required <= cols:
                return table, cols
    matches = []
    for table in sorted(tables):
        cols = table_columns(conn, table)
        if required <= cols:
            matches.append((table, cols))
    if len(matches) == 1:
        return matches[0]
    raise RuntimeError(f"No unambiguous table has required columns {sorted(required)}. Found tables: {sorted(tables)}")


def snapshot_db(source: Path) -> Path:
    if not source.exists():
        raise FileNotFoundError(f"Fregata database not found: {source}")
    fd, tmp_name = tempfile.mkstemp(prefix="frigate-snapshot-", suffix=".db")
    os.close(fd)
    tmp = Path(tmp_name)
    try:
        src = sqlite3.connect(f"file:{source}?mode=ro", uri=True, timeout=30)
        dst = sqlite3.connect(tmp)
        try:
            src.backup(dst)
        finally:
            dst.close()
            src.close()
    except BaseException:
        # A failed snapshot must not strand a copy of the NVR database in $TMPDIR: under
        # watch mode a persistent failure would repeat every poll until the disk fills.
        tmp.unlink(missing_ok=True)
        raise
    return tmp


def open_state(path: Path) -> sqlite3.Connection:
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.executescript("""
      CREATE TABLE IF NOT EXISTS event_delivery (
        event_id TEXT PRIMARY KEY,
        camera TEXT NOT NULL,
        start_time REAL NOT NULL,
        end_time REAL NOT NULL,
        manifest_key TEXT,
        completed_at REAL,
        last_error TEXT,
        updated_at REAL NOT NULL
      );
      CREATE TABLE IF NOT EXISTS segment_delivery (
        event_id TEXT NOT NULL,
        source_path TEXT NOT NULL,
        s3_key TEXT NOT NULL,
        etag TEXT,
        uploaded_at REAL,
        PRIMARY KEY(event_id, source_path)
      );
      CREATE TABLE IF NOT EXISTS notion_delivery (
        event_id TEXT PRIMARY KEY,
        page_id TEXT,
        synced_at REAL,
        last_error TEXT,
        attempts INTEGER NOT NULL DEFAULT 0,
        clip_signed_at REAL,
        clip_attempts INTEGER NOT NULL DEFAULT 0,
        updated_at REAL NOT NULL
      );
    """)
    # CREATE TABLE IF NOT EXISTS never alters an existing table, so a state DB written by an
    # older build keeps its old shape. This only fires for those; a fresh DB is already correct.
    if "attempts" not in table_columns(conn, "notion_delivery"):
        conn.execute("ALTER TABLE notion_delivery ADD COLUMN attempts INTEGER NOT NULL DEFAULT 0")
    # Clip link state. NULL clip_signed_at on an already-synced row is exactly what makes the
    # pre-clip-links backlog eligible for its first signed link once CLIP_LINKS is enabled.
    if "clip_signed_at" not in table_columns(conn, "notion_delivery"):
        conn.execute("ALTER TABLE notion_delivery ADD COLUMN clip_signed_at REAL")
    if "clip_attempts" not in table_columns(conn, "notion_delivery"):
        conn.execute("ALTER TABLE notion_delivery ADD COLUMN clip_attempts INTEGER NOT NULL DEFAULT 0")
    conn.commit()
    return conn


def parse_json(value: Any) -> Any:
    if not isinstance(value, str):
        return value
    try:
        return json.loads(value)
    except json.JSONDecodeError:
        return value


def fetch_events(conn: sqlite3.Connection, table: str, columns: set[str], settings: Settings) -> list[dict[str, Any]]:
    select_cols = [c for c in ("id", "camera", "label", "sub_label", "start_time", "end_time", "top_score", "false_positive", "zones", "has_clip", "has_snapshot", "data") if c in columns]
    where = ["end_time IS NOT NULL", "label = ?"]
    params: list[Any] = [settings.label]
    if settings.camera:
        where.append("camera = ?")
        params.append(settings.camera)
    sql = f"SELECT {', '.join(qident(c) for c in select_cols)} FROM {qident(table)} WHERE {' AND '.join(where)} ORDER BY start_time ASC"
    rows = []
    for row in conn.execute(sql, params):
        item = dict(row)
        for key in ("data", "zones", "sub_label"):
            if key in item:
                item[key] = parse_json(item[key])
        rows.append(item)
    return rows


def fetch_segments(conn: sqlite3.Connection, table: str, columns: set[str], camera: str, start: float, end: float) -> list[dict[str, Any]]:
    select_cols = [c for c in ("id", "camera", "path", "start_time", "end_time", "duration", "objects", "motion", "regions", "segment_size") if c in columns]
    sql = f"""SELECT {', '.join(qident(c) for c in select_cols)}
              FROM {qident(table)}
             WHERE camera = ? AND end_time >= ? AND start_time <= ?
             ORDER BY start_time ASC"""
    return [dict(r) for r in conn.execute(sql, (camera, start, end))]


def canonical_path(raw_path: str, recordings_dir: Path) -> Path:
    p = Path(raw_path)
    if p.exists():
        return p.resolve()
    candidates = [recordings_dir / p, recordings_dir / p.name]
    parts = p.parts
    if "recordings" in parts:
        i = parts.index("recordings")
        candidates.insert(0, recordings_dir.joinpath(*parts[i + 1:]))
    for candidate in candidates:
        if candidate.exists():
            return candidate.resolve()
    return candidates[0].resolve()


def relative_recording_key(path: Path, recordings_dir: Path) -> str:
    try:
        rel = path.relative_to(recordings_dir.resolve()).as_posix()
    except ValueError:
        digest = hashlib.sha256(str(path).encode()).hexdigest()[:12]
        rel = f"external/{digest}-{path.name}"
    return rel


def s3_client(settings: Settings):
    return boto3.client(
        "s3",
        region_name=settings.region,
        endpoint_url=settings.endpoint_url,
        # s3v4 explicitly: without it presigning can fall back to the deprecated SigV2,
        # which newer regions reject outright. Uploads are unaffected either way.
        config=BotoConfig(signature_version="s3v4",
                          retries={"max_attempts": 8, "mode": "standard"}),
    )


def clip_signer_client(settings: Settings):
    """The optional dedicated signing identity for clip links.

    Presigned URLs die with the credentials that signed them, so signing with a separate
    read-only IAM user turns deactivating that one key into an instant kill switch for
    every link ever written into Notion — without stopping the upload pipeline. Returns
    None when not configured; callers then sign with the regular client.
    """
    if not (settings.clip_aws_access_key_id and settings.clip_aws_secret_access_key):
        return None
    return boto3.client(
        "s3",
        region_name=settings.region,
        endpoint_url=settings.endpoint_url,
        aws_access_key_id=settings.clip_aws_access_key_id,
        aws_secret_access_key=settings.clip_aws_secret_access_key,
        config=BotoConfig(signature_version="s3v4",
                          retries={"max_attempts": 8, "mode": "standard"}),
    )


def upload_file(client, settings: Settings, source: Path, key: str) -> str | None:
    if settings.dry_run:
        LOG.info("DRY RUN upload %s -> s3://%s/%s", source, settings.bucket, key)
        return None
    if not settings.bucket:
        raise RuntimeError("S3_BUCKET is required when DRY_RUN=false")
    content_type = mimetypes.guess_type(source.name)[0] or "application/octet-stream"
    client.upload_file(str(source), settings.bucket, key, ExtraArgs={"ContentType": content_type})
    head = client.head_object(Bucket=settings.bucket, Key=key)
    return str(head.get("ETag", "")).strip('"') or None


def upload_manifest(client, settings: Settings, key: str, manifest: dict[str, Any]) -> None:
    body = json.dumps(manifest, indent=2, sort_keys=True, ensure_ascii=False).encode()
    if settings.dry_run:
        LOG.info("DRY RUN upload manifest -> s3://%s/%s\n%s", settings.bucket, key, body.decode())
        return
    client.put_object(Bucket=settings.bucket, Key=key, Body=body, ContentType="application/json")


NOTION_API = "https://api.notion.com/v1"
_NOTION_TARGETS: tuple[dict[str, Any], str] | None = None


class NotionError(RuntimeError):
    def __init__(self, message: str, status: int | None):
        super().__init__(message)
        self.status = status

    @property
    def blames_this_event(self) -> bool:
        # A 4xx that is not a rate limit means Notion rejected THIS page — bad property, bad
        # value. Anything else (429, 5xx, timeout, connection error) is the service or the
        # network, and must not count against one event's attempt budget.
        return self.status is not None and 400 <= self.status < 500 and self.status != 429


def notion_request(method: str, path: str, settings: Settings, payload: dict[str, Any] | None = None) -> dict[str, Any]:
    headers = {
        "Authorization": f"Bearer {settings.notion_token}",
        "Notion-Version": settings.notion_version,
        "Content-Type": "application/json",
    }
    for attempt in range(4):
        resp = requests.request(method, NOTION_API + path, headers=headers, json=payload, timeout=30)
        # 429 is the only safe blind retry: Notion rejected the call outright, so nothing was created.
        if resp.status_code == 429 and attempt < 3:
            time.sleep(retry_after_seconds(resp.headers.get("Retry-After"), attempt))
            continue
        if resp.status_code >= 400:
            raise NotionError(f"Notion {method} {path} failed {resp.status_code}: {resp.text[:300]}",
                              resp.status_code)
        return resp.json()
    raise NotionError(f"Notion {method} {path} exhausted retries", None)


def retry_after_seconds(header: str | None, attempt: int) -> float:
    # Retry-After may be absent, a non-numeric HTTP date, or "0". Fall back to exponential
    # backoff, and cap the sleep so one bad header cannot stall the whole poll loop.
    try:
        value = float(header)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        value = float(2 ** attempt)
    if value <= 0:
        value = float(2 ** attempt)
    return min(max(value, 1.0), 60.0)


def notion_targets(settings: Settings) -> tuple[dict[str, Any], str]:
    # Notion 2025-09-03 split databases into data sources: pages parent to a data_source_id and
    # querying moved off /databases/{id}/query. Resolve once and fall back to the older shape so
    # this works on either side of that change.
    global _NOTION_TARGETS
    if _NOTION_TARGETS is None:
        db = notion_request("GET", f"/databases/{settings.notion_database_id}", settings)
        sources = db.get("data_sources") or []
        if sources:
            ds = str(sources[0]["id"])
            _NOTION_TARGETS = ({"data_source_id": ds}, f"/data_sources/{ds}/query")
        else:
            _NOTION_TARGETS = ({"database_id": settings.notion_database_id},
                               f"/databases/{settings.notion_database_id}/query")
        LOG.info("Notion parent resolved to %s", _NOTION_TARGETS[0])
    return _NOTION_TARGETS


def notion_properties(event: dict[str, Any], manifest_key: str | None, segments: int, settings: Settings) -> dict[str, Any]:
    # sub_label is a real person's name. Publishing it is opt-in, and a comma is not a legal
    # Notion select option, so strip it rather than let one enrolled name fail every run.
    if settings.notion_include_person:
        sub = event.get("sub_label")
        person = (sub if isinstance(sub, str) else "").replace(",", " ").strip() or "Unrecognized"
    else:
        person = "Unrecognized"
    start = float(event["start_time"])
    end = float(event["end_time"])
    def local_iso(ts: float) -> str:
        return datetime.fromtimestamp(ts, timezone.utc).astimezone().isoformat()
    props: dict[str, Any] = {
        "Event ID": {"title": [{"text": {"content": str(event["id"])}}]},
        "Person": {"select": {"name": person}},
        "Camera": {"select": {"name": str(event["camera"])}},
        "Seen": {"date": {"start": local_iso(start), "end": local_iso(end)}},
        "Duration (s)": {"number": round(end - start, 1)},
        "Segments": {"number": segments},
        "Manifest key": {"rich_text": [{"text": {"content": manifest_key or ""}}]},
    }
    if event.get("top_score") is not None:
        props["Score"] = {"number": round(float(event["top_score"]), 3)}
    return props


def record_notion_error(state: sqlite3.Connection, event_id: str, exc: Exception) -> None:
    state.execute("""INSERT INTO notion_delivery(event_id,last_error,updated_at)
                     VALUES(?,?,?)
                     ON CONFLICT(event_id) DO UPDATE SET last_error=excluded.last_error,updated_at=excluded.updated_at""",
                  (event_id, str(exc), time.time()))
    state.commit()


# --------------------------------------------------------------------------
# Clip links: a presigned viewer page in S3, its URL kept fresh in Notion.
#
# The Clip column holds one presigned GET URL for a small HTML page in the
# bucket that plays every delivered segment of the event (each <video> src is
# itself a presigned URL, signed in the same batch with the same TTL). SigV4
# signatures live at most 7 days, so a refresh pass inside the normal poll
# loop re-signs and re-PATCHes any link older than CLIP_REFRESH_SECONDS. The
# link is a bearer capability: anyone who can see the Notion page can watch
# that one event until the signature expires. Nothing here is a server, and
# nothing new listens on any port.
# --------------------------------------------------------------------------

CLIP_REFRESH_BATCH = 25       # rows per pass: drains a 312-page backlog in minutes
CLIP_PATCH_SPACING = 0.34     # seconds between PATCHes: <1 req/s of Notion's ~3

CLIP_PAGE = """<!doctype html><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<meta name="robots" content="noindex">
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
</style>
<h1>{event_id}</h1>
{body}
"""


def render_player(event_id: str, videos: list[tuple[str, str]]) -> str:
    """The viewer page: one <video> per delivered segment, in key order.

    Escaping matters even for our own data: presigned URLs contain '&', which is
    invalid raw inside an attribute, and there is no way to attach a CSP to an
    object served straight from S3.
    """
    segs = "\n".join(
        '<div class="seg"><div class="lbl">{n}</div>'
        '<video controls preload="metadata" src="{u}"></video></div>'.format(
            n=html.escape(label), u=html.escape(url, quote=True))
        for label, url in videos)
    return CLIP_PAGE.format(event_id=html.escape(event_id), body=segs)


def presign_get(client, settings: Settings, key: str) -> str:
    return client.generate_presigned_url(
        "get_object",
        Params={"Bucket": settings.bucket, "Key": key},
        ExpiresIn=settings.clip_url_ttl,
    )


def clip_page_key(settings: Settings, camera: str, event_id: str) -> str:
    rel = f"events/{camera}/{event_id}/index.html"
    return f"{settings.prefix}/{rel}" if settings.prefix else rel


_PRESIGN_CAP_WARNED = False


def warn_if_presign_capped(settings: Settings) -> None:
    """Session credentials silently cap presign lifetime at the session's, not the TTL.

    With STS/SSO credentials every clip link dies when the session does — hours, not
    days — and nothing errors at signing time. Warn once; dedicated CLIP_AWS_* keys
    are long-term by construction, so they skip the check.
    """
    global _PRESIGN_CAP_WARNED
    if _PRESIGN_CAP_WARNED or settings.clip_aws_access_key_id:
        return
    _PRESIGN_CAP_WARNED = True
    try:
        creds = boto3.Session().get_credentials()
        token = creds.get_frozen_credentials().token if creds else None
    except Exception:
        return
    if token:
        LOG.warning("AWS credentials carry a session token: presigned clip links will expire "
                    "with the session, regardless of CLIP_URL_TTL_SECONDS=%d. Use long-term "
                    "IAM user keys (or CLIP_AWS_ACCESS_KEY_ID) for clip links.",
                    settings.clip_url_ttl)


def refresh_one_clip(row: sqlite3.Row, state: sqlite3.Connection, client, signer, settings: Settings) -> None:
    """Sign a fresh viewer page for one event and PATCH its URL into Notion.

    Shares the creation path's temperament: never raises, never touches synced_at, and
    budgets failures with the same blame rules — a non-429 4xx is this page's fault and
    charges clip_attempts; 429/5xx/network says nothing and is refunded. Unlike the
    creation budget, clip_attempts resets to 0 on success: refresh recurs forever, so a
    lifetime budget would eventually retire every row over accumulated bad luck.
    """
    event_id = row["event_id"]
    clip_attempts = int(row["clip_attempts"])

    keys = [r["s3_key"] for r in state.execute(
        "SELECT s3_key FROM segment_delivery WHERE event_id=? AND uploaded_at IS NOT NULL ORDER BY s3_key",
        (event_id,))]
    if not keys:
        # A synced page with no delivered segments can never get a working link; charge it
        # so it retires instead of looping forever, with a diagnosis in last_error.
        state.execute("UPDATE notion_delivery SET clip_attempts=?,updated_at=? WHERE event_id=?",
                      (clip_attempts + 1, time.time(), event_id))
        record_notion_error(state, event_id,
                            RuntimeError(f"clip link: no delivered segments recorded for event {event_id}"))
        LOG.warning("Clip link found no delivered segments for event %s (attempt %s/%s)",
                    event_id, clip_attempts + 1, settings.notion_max_attempts)
        return

    page_id = row["page_id"]
    if page_id is None:
        # A synced row should always carry its page_id, but be defensive: re-find the page
        # with the same 'Event ID' dedupe query the creation path uses.
        try:
            _, query_path = notion_targets(settings)
            found = notion_request("POST", query_path, settings,
                                   {"filter": {"property": "Event ID", "title": {"equals": event_id}}})
            results = found.get("results") or []
            page_id = str(results[0]["id"]) if results else None
        except Exception as exc:
            # The lookup failing is the network or the workspace, not this event — record
            # it but charge nothing, mirroring the blame semantics of the creation path.
            record_notion_error(state, event_id, exc)
            LOG.warning("Clip link page lookup failed for event %s; no attempt charged: %s", event_id, exc)
            return
        if page_id is None:
            # The lookup ran and found nothing: a synced row whose page cannot be found
            # will never succeed, so this IS the event's fault — charge the attempt.
            state.execute("UPDATE notion_delivery SET clip_attempts=?,updated_at=? WHERE event_id=?",
                          (clip_attempts + 1, time.time(), event_id))
            record_notion_error(state, event_id,
                                RuntimeError(f"clip link: no Notion page found for event {event_id}"))
            LOG.warning("Clip link found no page for event %s (attempt %s/%s)",
                        event_id, clip_attempts + 1, settings.notion_max_attempts)
            return

    # Pre-charge the attempt before any network call, consistent with the creation path.
    # put_object and PATCH are both idempotent, so a crash here costs one attempt — which
    # the next success refunds wholesale by resetting the counter — never a duplicate.
    # page_id rides along so an id recovered by the lookup above survives a failure.
    state.execute("UPDATE notion_delivery SET page_id=?,clip_attempts=?,updated_at=? WHERE event_id=?",
                  (page_id, clip_attempts + 1, time.time(), event_id))
    state.commit()
    try:
        videos = [(k.rsplit("/", 1)[-1], presign_get(signer, settings, k)) for k in keys]
        page_key = clip_page_key(settings, str(row["camera"]), event_id)
        client.put_object(Bucket=settings.bucket, Key=page_key,
                          Body=render_player(event_id, videos).encode(),
                          ContentType="text/html; charset=utf-8",
                          CacheControl="no-store")
        page_url = presign_get(signer, settings, page_key)
        notion_request("PATCH", f"/pages/{page_id}", settings,
                       {"properties": {"Clip": {"url": page_url}}})
    except Exception as exc:
        # Deliberately NOT re-raised: the page itself is fine, only its link is stale.
        blamed = isinstance(exc, NotionError) and exc.blames_this_event
        if not blamed:
            # Refund: a 429, 5xx, dropped connection or S3 hiccup says nothing about
            # whether this page will ever accept a Clip URL.
            state.execute("UPDATE notion_delivery SET clip_attempts=? WHERE event_id=?",
                          (clip_attempts, event_id))
        record_notion_error(state, event_id, exc)
        LOG.warning("Clip link failed for event %s (attempt %s): %s",
                    event_id, f"{clip_attempts + 1}/{settings.notion_max_attempts}" if blamed else "not charged", exc)
        return
    now = time.time()
    state.execute("""UPDATE notion_delivery
                        SET page_id=?,clip_signed_at=?,clip_attempts=0,last_error=NULL,updated_at=?
                      WHERE event_id=?""",
                  (page_id, now, now, event_id))
    state.commit()
    LOG.info("Clip link refreshed on page %s for event %s", page_id, event_id)


def refresh_clip_links(state: sqlite3.Connection, client, settings: Settings) -> None:
    """One refresh pass: sign the never-linked and the stale, oldest first.

    Driven entirely by the state DB, so pages keep their links long after Fregata's
    retention has evicted the source events. NULL clip_signed_at sorts before every
    real timestamp under ASC, so the backlog and brand-new pages always beat routine
    re-signs; the batch cap and spacing keep the initial heal under Notion's rate
    limit and off the critical path of actual deliveries.
    """
    if settings.dry_run or not settings.clip_links or not settings.notion_token \
            or not settings.notion_database_id or not settings.bucket:
        return
    warn_if_presign_capped(settings)
    signer = clip_signer_client(settings) or client
    cutoff = time.time() - settings.clip_refresh_seconds
    rows = state.execute("""
        SELECT n.event_id, n.page_id, n.clip_attempts, e.camera
          FROM notion_delivery n JOIN event_delivery e ON e.event_id = n.event_id
         WHERE n.synced_at IS NOT NULL
           AND e.completed_at IS NOT NULL
           AND (n.clip_signed_at IS NULL OR n.clip_signed_at < ?)
           AND n.clip_attempts < ?
         ORDER BY n.clip_signed_at ASC
         LIMIT ?""", (cutoff, settings.notion_max_attempts, CLIP_REFRESH_BATCH)).fetchall()
    for i, row in enumerate(rows):
        if i:
            time.sleep(CLIP_PATCH_SPACING)
        refresh_one_clip(row, state, client, signer, settings)


def sync_notion(event: dict[str, Any], manifest_key: str | None, segments: int, state: sqlite3.Connection, settings: Settings) -> None:
    if settings.dry_run or not settings.notion_token or not settings.notion_database_id:
        return
    event_id = str(event["id"])
    row = state.execute("SELECT synced_at,attempts FROM notion_delivery WHERE event_id=?", (event_id,)).fetchone()
    if row and row["synced_at"] is not None:
        return
    attempts = int(row["attempts"]) if row else 0
    if attempts >= settings.notion_max_attempts:
        return  # terminal: stop hammering Notion every poll for an event that will never sync

    # Resolve the workspace target BEFORE touching the attempt counter. A failure here is a
    # wrong NOTION_DATABASE_ID, an integration that was never shared, an expired token, or
    # Notion being down — none of which is this event's fault. Counting it would let a few
    # minutes of misconfiguration permanently retire the entire backlog.
    try:
        parent, query_path = notion_targets(settings)
    except Exception as exc:
        record_notion_error(state, event_id, exc)
        LOG.warning("Notion unreachable (%s); no attempt charged to event %s", exc, event_id)
        return

    # Record the attempt BEFORE the network call. A crash between creating the page and writing
    # the result would otherwise leave no row, and the next pass would create a duplicate page.
    state.execute("""INSERT INTO notion_delivery(event_id,attempts,updated_at)
                     VALUES(?,?,?)
                     ON CONFLICT(event_id) DO UPDATE SET attempts=excluded.attempts,updated_at=excluded.updated_at""",
                  (event_id, attempts + 1, time.time()))
    state.commit()
    try:
        page_id = None
        if row:  # a prior attempt may have created a page before failing; no row means we never tried
            try:
                found = notion_request("POST", query_path, settings,
                                       {"filter": {"property": "Event ID", "title": {"equals": event_id}}})
                results = found.get("results") or []
                page_id = str(results[0]["id"]) if results else None
            except Exception:
                # A dedupe lookup that cannot run must not block the sync forever. Worst case we
                # create a duplicate page, which is far better than never syncing at all.
                LOG.warning("Notion dedupe lookup failed for %s; creating a page anyway", event_id)
        if page_id is None:
            page = notion_request("POST", "/pages", settings, {
                "parent": parent,
                "properties": notion_properties(event, manifest_key, segments, settings),
            })
            page_id = str(page["id"])
    except Exception as exc:
        # Deliberately NOT re-raised. Notion is a secondary sink; letting this reach run_once
        # would write last_error into event_delivery, which tracks the S3 upload that succeeded,
        # and nothing clears that flag again for an already-completed event.
        blamed = isinstance(exc, NotionError) and exc.blames_this_event
        if not blamed:
            # Refund the attempt: a 429, a 5xx or a dropped connection says nothing about
            # whether this event will ever sync, so it must not count toward giving up.
            state.execute("UPDATE notion_delivery SET attempts=? WHERE event_id=?", (attempts, event_id))
        record_notion_error(state, event_id, exc)
        LOG.warning("Notion sync failed for event %s (attempt %s): %s",
                    event_id, f"{attempts + 1}/{settings.notion_max_attempts}" if blamed else "not charged", exc)
        return
    now = time.time()
    state.execute("""INSERT INTO notion_delivery(event_id,page_id,synced_at,last_error,updated_at)
                     VALUES(?,?,?,NULL,?)
                     ON CONFLICT(event_id) DO UPDATE SET page_id=excluded.page_id,synced_at=excluded.synced_at,last_error=NULL,updated_at=excluded.updated_at""",
                  (event_id, page_id, now, now))
    state.commit()
    LOG.info("Notion page %s for event %s", page_id, event_id)


def process_event(event: dict[str, Any], fconn: sqlite3.Connection, recording_table: str, recording_cols: set[str], state: sqlite3.Connection, client, settings: Settings) -> None:
    event_id = str(event["id"])
    done = state.execute("SELECT completed_at,manifest_key FROM event_delivery WHERE event_id=?", (event_id,)).fetchone()
    if done and done["completed_at"] is not None:
        segments_done = state.execute("SELECT COUNT(*) FROM segment_delivery WHERE event_id=? AND uploaded_at IS NOT NULL", (event_id,)).fetchone()[0]
        sync_notion(event, done["manifest_key"], segments_done, state, settings)
        return
    start = float(event["start_time"]) - settings.pre_roll
    end = float(event["end_time"]) + settings.post_roll
    segments = fetch_segments(fconn, recording_table, recording_cols, str(event["camera"]), start, end)
    now = time.time()
    state.execute("""INSERT INTO event_delivery(event_id,camera,start_time,end_time,updated_at)
                     VALUES(?,?,?,?,?)
                     ON CONFLICT(event_id) DO UPDATE SET updated_at=excluded.updated_at,last_error=NULL""",
                  (event_id, event["camera"], event["start_time"], event["end_time"], now))
    state.commit()
    if not segments:
        raise RuntimeError(f"No recording segments overlap event {event_id} window {utc_iso(start)} to {utc_iso(end)}")

    uploaded = []
    for seg in segments:
        path = canonical_path(str(seg["path"]), settings.recordings_dir)
        if not path.exists():
            raise FileNotFoundError(f"Indexed recording does not exist: {path}")
        age = time.time() - path.stat().st_mtime
        if age < settings.settle_seconds:
            raise RuntimeError(f"Recording is still settling ({age:.1f}s old): {path}")
        rel = relative_recording_key(path, settings.recordings_dir)
        key = f"{settings.prefix}/recordings/{rel}" if settings.prefix else f"recordings/{rel}"
        previous = state.execute("SELECT uploaded_at,etag FROM segment_delivery WHERE event_id=? AND source_path=?", (event_id, str(path))).fetchone()
        etag = previous["etag"] if previous and previous["uploaded_at"] else upload_file(client, settings, path, key)
        if not settings.dry_run and not (previous and previous["uploaded_at"]):
            state.execute("""INSERT INTO segment_delivery(event_id,source_path,s3_key,etag,uploaded_at)
                             VALUES(?,?,?,?,?)
                             ON CONFLICT(event_id,source_path) DO UPDATE SET s3_key=excluded.s3_key,etag=excluded.etag,uploaded_at=excluded.uploaded_at""",
                          (event_id, str(path), key, etag, time.time()))
            state.commit()
        uploaded.append({
            "source_path": str(path), "s3_key": key,
            "start_time": seg.get("start_time"), "start_time_utc": utc_iso(seg.get("start_time")),
            "end_time": seg.get("end_time"), "end_time_utc": utc_iso(seg.get("end_time")),
            "etag": etag,
        })

    manifest_key = f"{settings.prefix}/events/{event['camera']}/{event_id}/manifest.json" if settings.prefix else f"events/{event['camera']}/{event_id}/manifest.json"
    manifest = {
        "schema_version": 1,
        "generated_at": utc_iso(time.time()),
        "source": "fregata-sqlite-reconciler",
        "event": {
            **event,
            "start_time_utc": utc_iso(event.get("start_time")),
            "end_time_utc": utc_iso(event.get("end_time")),
        },
        "archive_window": {
            "start_time": start, "start_time_utc": utc_iso(start),
            "end_time": end, "end_time_utc": utc_iso(end),
            "pre_roll_seconds": settings.pre_roll,
            "post_roll_seconds": settings.post_roll,
        },
        "segments": uploaded,
    }
    if settings.upload_manifest:
        upload_manifest(client, settings, manifest_key, manifest)
    if not settings.dry_run:
        state.execute("UPDATE event_delivery SET manifest_key=?,completed_at=?,last_error=NULL,updated_at=? WHERE event_id=?",
                      (manifest_key if settings.upload_manifest else None, time.time(), time.time(), event_id))
        state.commit()
    LOG.info("%s event %s with %d segment(s)", "Planned" if settings.dry_run else "Completed", event_id, len(uploaded))
    sync_notion(event, manifest_key if settings.upload_manifest else None, len(uploaded), state, settings)


def inspect(settings: Settings) -> int:
    snap = snapshot_db(settings.source_db)
    try:
        conn = sqlite3.connect(snap)
        event_table, event_cols = resolve_table(conn, ("event", "events"), EVENT_REQUIRED)
        recording_table, recording_cols = resolve_table(conn, ("recordings", "recording"), RECORDING_REQUIRED)
        print(json.dumps({
            "source_db": str(settings.source_db),
            "event_table": event_table, "event_columns": sorted(event_cols),
            "recording_table": recording_table, "recording_columns": sorted(recording_cols),
        }, indent=2))
        return 0
    finally:
        try: conn.close()
        except Exception: pass
        snap.unlink(missing_ok=True)


def run_once(settings: Settings) -> int:
    snap = snapshot_db(settings.source_db)
    state = open_state(settings.state_db)
    client = s3_client(settings)
    failures = 0
    try:
        fconn = sqlite3.connect(snap)
        fconn.row_factory = sqlite3.Row
        event_table, event_cols = resolve_table(fconn, ("event", "events"), EVENT_REQUIRED)
        recording_table, recording_cols = resolve_table(fconn, ("recordings", "recording"), RECORDING_REQUIRED)
        events = fetch_events(fconn, event_table, event_cols, settings)
        LOG.info("Found %d finalized event(s) matching filters", len(events))
        for event in events:
            try:
                process_event(event, fconn, recording_table, recording_cols, state, client, settings)
            except Exception as exc:
                failures += 1
                LOG.exception("Event %s failed", event.get("id"))
                if event.get("id"):
                    state.execute("UPDATE event_delivery SET last_error=?,updated_at=? WHERE event_id=?", (str(exc), time.time(), str(event["id"])))
                    state.commit()
        # After the deliveries, so a slow Notion day can never delay footage leaving the
        # house. New pages picked up above get their first link in this same pass.
        refresh_clip_links(state, client, settings)
        return 1 if failures else 0
    finally:
        try: fconn.close()
        except Exception: pass
        state.close()
        snap.unlink(missing_ok=True)


def status(settings: Settings) -> int:
    state = open_state(settings.state_db)
    try:
        total = state.execute("SELECT COUNT(*) FROM event_delivery").fetchone()[0]
        complete = state.execute("SELECT COUNT(*) FROM event_delivery WHERE completed_at IS NOT NULL").fetchone()[0]
        failed = state.execute("SELECT COUNT(*) FROM event_delivery WHERE last_error IS NOT NULL").fetchone()[0]
        segments = state.execute("SELECT COUNT(*) FROM segment_delivery WHERE uploaded_at IS NOT NULL").fetchone()[0]
        notion_synced = state.execute("SELECT COUNT(*) FROM notion_delivery WHERE synced_at IS NOT NULL").fetchone()[0]
        notion_failed = state.execute("SELECT COUNT(*) FROM notion_delivery WHERE synced_at IS NULL AND last_error IS NOT NULL").fetchone()[0]
        notion_gaveup = state.execute("SELECT COUNT(*) FROM notion_delivery WHERE synced_at IS NULL AND attempts>=?", (settings.notion_max_attempts,)).fetchone()[0]
        # Clip refresh progress. Only synced pages count: an unsynced event has no page to
        # hold a link, so its missing clip is the creation path's problem, not refresh's.
        fresh_cutoff = time.time() - settings.clip_refresh_seconds
        clip_fresh = state.execute("SELECT COUNT(*) FROM notion_delivery WHERE clip_signed_at IS NOT NULL AND clip_signed_at >= ?", (fresh_cutoff,)).fetchone()[0]
        clip_stale = state.execute("SELECT COUNT(*) FROM notion_delivery WHERE synced_at IS NOT NULL AND (clip_signed_at IS NULL OR clip_signed_at < ?) AND clip_attempts < ?",
                                   (fresh_cutoff, settings.notion_max_attempts)).fetchone()[0]
        clip_gaveup = state.execute("SELECT COUNT(*) FROM notion_delivery WHERE synced_at IS NOT NULL AND clip_attempts >= ?", (settings.notion_max_attempts,)).fetchone()[0]
        print(json.dumps({"events_seen": total, "events_complete": complete, "events_failed": failed, "segments_uploaded": segments,
                          "notion_synced": notion_synced, "notion_failed": notion_failed, "notion_gave_up": notion_gaveup,
                          "clip_fresh": clip_fresh, "clip_stale": clip_stale, "clip_gave_up": clip_gaveup,
                          "dry_run": settings.dry_run}, indent=2))
        for row in state.execute("SELECT event_id,last_error FROM event_delivery WHERE last_error IS NOT NULL ORDER BY updated_at DESC LIMIT 10"):
            print(f"FAILED {row['event_id']}: {row['last_error']}")
        for row in state.execute("SELECT event_id,last_error FROM notion_delivery WHERE synced_at IS NULL AND last_error IS NOT NULL ORDER BY updated_at DESC LIMIT 10"):
            print(f"NOTION FAILED {row['event_id']}: {row['last_error']}")
        # Clip failures live on SYNCED rows, which the listing above excludes by design —
        # without this, clip_gave_up climbing would be a number with no diagnosis attached.
        for row in state.execute("SELECT event_id,last_error FROM notion_delivery WHERE synced_at IS NOT NULL AND last_error IS NOT NULL ORDER BY updated_at DESC LIMIT 10"):
            print(f"CLIP FAILED {row['event_id']}: {row['last_error']}")
        return 0
    finally:
        state.close()


def clips_reset(settings: Settings) -> int:
    """Forget all clip link state so the next pass re-signs everything.

    The recovery gesture for both dead ends: a Notion DB that was missing the Clip
    property (every page's clip budget burned before the fix) and a rotated IAM key
    (every outstanding signature invalidated at once). Costs nothing but PATCHes.
    """
    state = open_state(settings.state_db)
    try:
        cur = state.execute("UPDATE notion_delivery SET clip_attempts=0, clip_signed_at=NULL WHERE synced_at IS NOT NULL")
        state.commit()
        print(f"Reset clip link state on {cur.rowcount} synced page(s); links re-sign on the next pass")
        return 0
    finally:
        state.close()


def main() -> int:
    parser = argparse.ArgumentParser(description="Reconcile Fregata events and recording segments to S3")
    parser.add_argument("command", choices=("inspect", "once", "watch", "status", "clips-reset"))
    args = parser.parse_args()
    settings = Settings.from_env()
    logging.basicConfig(level=os.getenv("LOG_LEVEL", "INFO").upper(), format="%(asctime)s %(levelname)s %(message)s")
    if args.command == "inspect": return inspect(settings)
    if args.command == "status": return status(settings)
    if args.command == "clips-reset": return clips_reset(settings)
    if args.command == "once": return run_once(settings)
    while True:
        try:
            run_once(settings)
        except KeyboardInterrupt:
            return 130
        except Exception:
            LOG.exception("Reconciliation pass failed")
        time.sleep(settings.poll_seconds)


if __name__ == "__main__":
    raise SystemExit(main())
