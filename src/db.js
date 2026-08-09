import Database from 'better-sqlite3';
import { mkdirSync } from 'node:fs';
import { dirname } from 'node:path';
import { config } from './config.js';

mkdirSync(dirname(config.dbPath), { recursive: true });

export const db = new Database(config.dbPath);
db.pragma('journal_mode = WAL');
db.pragma('foreign_keys = ON');

db.exec(`
CREATE TABLE IF NOT EXISTS meta (
  key        TEXT PRIMARY KEY,
  value      TEXT NOT NULL,
  updated_at INTEGER NOT NULL
);

-- One row per Frigate tracked-object event. Upserted by id, so replaying a
-- poll window is harmless.
CREATE TABLE IF NOT EXISTS events (
  id              TEXT PRIMARY KEY,
  camera          TEXT NOT NULL,
  label           TEXT NOT NULL,
  sub_label       TEXT,              -- recognised name; NULL => unknown person
  sub_label_score REAL,
  start_time      REAL NOT NULL,
  end_time        REAL,              -- NULL while still in progress
  has_clip        INTEGER NOT NULL DEFAULT 0,
  has_snapshot    INTEGER NOT NULL DEFAULT 0,
  session_id      TEXT,
  raw             TEXT NOT NULL,
  first_seen_at   INTEGER NOT NULL,
  updated_at      INTEGER NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_events_session ON events(session_id);
CREATE INDEX IF NOT EXISTS idx_events_open    ON events(end_time) WHERE end_time IS NULL;

-- Unlock events pushed by Home Assistant.
CREATE TABLE IF NOT EXISTS unlocks (
  id         TEXT PRIMARY KEY,       -- idempotency key from HA
  ts         REAL NOT NULL,
  source     TEXT,
  session_id TEXT,
  raw        TEXT,
  created_at INTEGER NOT NULL
);

-- One row per entry episode. This is what becomes a Notion row.
CREATE TABLE IF NOT EXISTS sessions (
  id               TEXT PRIMARY KEY,
  kind             TEXT NOT NULL,     -- entry | unlock_no_camera | gap
  opened_at        REAL NOT NULL,
  window_until     REAL NOT NULL,
  closed_at        REAL,
  people_known     TEXT,              -- JSON array of names
  count_total      INTEGER NOT NULL DEFAULT 0,
  count_unknown    INTEGER NOT NULL DEFAULT 0,
  has_unlock       INTEGER NOT NULL DEFAULT 0,
  gap_from         REAL,              -- kind='gap' only
  gap_to           REAL,
  -- publish state, tracked per sink so a failure in one doesn't re-fire another
  clips_uploaded   INTEGER NOT NULL DEFAULT 0,
  notion_page_id   TEXT,
  slack_ts         TEXT,
  publish_attempts INTEGER NOT NULL DEFAULT 0,
  next_attempt_at  INTEGER,
  last_error       TEXT,
  created_at       INTEGER NOT NULL,
  updated_at       INTEGER NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_sessions_open      ON sessions(closed_at) WHERE closed_at IS NULL;
CREATE INDEX IF NOT EXISTS idx_sessions_publishing ON sessions(closed_at, notion_page_id);

CREATE TABLE IF NOT EXISTS artifacts (
  event_id    TEXT NOT NULL,
  kind        TEXT NOT NULL,          -- clip | snapshot
  s3_key      TEXT NOT NULL,
  bytes       INTEGER,
  uploaded_at INTEGER NOT NULL,
  PRIMARY KEY (event_id, kind)
);

-- Audit trail: who viewed which clip, when. Turns "access is limited to admins"
-- into an actual control.
CREATE TABLE IF NOT EXISTS clip_views (
  id         INTEGER PRIMARY KEY AUTOINCREMENT,
  session_id TEXT,
  event_id   TEXT,
  viewer     TEXT,
  ip         TEXT,
  ts         INTEGER NOT NULL
);
`);

export function getMeta(key, dflt = null) {
  const row = db.prepare('SELECT value FROM meta WHERE key = ?').get(key);
  return row ? row.value : dflt;
}

export function setMeta(key, value) {
  db.prepare(
    `INSERT INTO meta (key, value, updated_at) VALUES (?, ?, ?)
     ON CONFLICT(key) DO UPDATE SET value = excluded.value, updated_at = excluded.updated_at`
  ).run(key, String(value), Date.now());
}

export const nowSec = () => Date.now() / 1000;
