#!/usr/bin/env python3
from __future__ import annotations

import argparse
import hashlib
import json
import logging
import mimetypes
import os
import shutil
import sqlite3
import sys
import tempfile
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable

import boto3
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

    @staticmethod
    def from_env() -> "Settings":
        load_dotenv()
        def b(name: str, default: bool) -> bool:
            return os.getenv(name, str(default)).strip().lower() in {"1", "true", "yes", "on"}
        camera = os.getenv("CAMERA", "").strip() or None
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
            upload_manifest=b("UPLOAD_EVENT_MANIFEST", True),
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
    src = sqlite3.connect(f"file:{source}?mode=ro", uri=True, timeout=30)
    dst = sqlite3.connect(tmp)
    try:
        src.backup(dst)
    finally:
        dst.close()
        src.close()
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
    """)
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
        config=BotoConfig(retries={"max_attempts": 8, "mode": "standard"}),
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


def process_event(event: dict[str, Any], fconn: sqlite3.Connection, recording_table: str, recording_cols: set[str], state: sqlite3.Connection, client, settings: Settings) -> None:
    event_id = str(event["id"])
    done = state.execute("SELECT completed_at FROM event_delivery WHERE event_id=?", (event_id,)).fetchone()
    if done and done["completed_at"] is not None:
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
        print(json.dumps({"events_seen": total, "events_complete": complete, "events_failed": failed, "segments_uploaded": segments, "dry_run": settings.dry_run}, indent=2))
        for row in state.execute("SELECT event_id,last_error FROM event_delivery WHERE last_error IS NOT NULL ORDER BY updated_at DESC LIMIT 10"):
            print(f"FAILED {row['event_id']}: {row['last_error']}")
        return 0
    finally:
        state.close()


def main() -> int:
    parser = argparse.ArgumentParser(description="Reconcile Fregata events and recording segments to S3")
    parser.add_argument("command", choices=("inspect", "once", "watch", "status"))
    args = parser.parse_args()
    settings = Settings.from_env()
    logging.basicConfig(level=os.getenv("LOG_LEVEL", "INFO").upper(), format="%(asctime)s %(levelname)s %(message)s")
    if args.command == "inspect": return inspect(settings)
    if args.command == "status": return status(settings)
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
