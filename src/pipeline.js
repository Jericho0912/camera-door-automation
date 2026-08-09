import { db, getMeta, setMeta, nowSec } from './db.js';
import { config } from './config.js';
import { log } from './log.js';
import * as frigate from './frigate.js';
import * as storage from './storage.js';
import { upsertEvent, closeDueSessions } from './sessions.js';
import { createNotionRow, postSlackAlert } from './sinks.js';

const CURSOR = 'cursor.start_time';

/**
 * One poll pass. Reads from the cursor minus an overlap, upserts everything it
 * finds, then advances. The cursor only moves after a successful write, so a
 * crash mid-pass replays rather than skips.
 */
export async function poll() {
  const cursor = Number(getMeta(CURSOR, nowSec() - 3600));
  const after = cursor - config.poll.overlapSeconds;

  let events;
  try {
    events = await frigate.listEvents({ after });
  } catch (err) {
    log.error('poll failed', { err: err.message });
    return { ok: false };
  }

  let maxStart = cursor;
  const tx = db.transaction((list) => {
    for (const raw of list) {
      upsertEvent(raw);
      if (raw.start_time > maxStart) maxStart = raw.start_time;
    }
  });
  tx(events);

  // Re-check events we've seen but that hadn't ended yet — they may have closed
  // and picked up a face match since.
  const open = db
    .prepare('SELECT id FROM events WHERE end_time IS NULL AND start_time > ?')
    .all(nowSec() - config.session.maxSeconds * 4);
  for (const { id } of open) {
    try {
      const fresh = await frigate.getEvent(id);
      if (fresh) upsertEvent(fresh);
    } catch (err) {
      if (err.status !== 404) log.warn('refresh failed', { event: id, err: err.message });
    }
  }

  setMeta(CURSOR, maxStart);
  setMeta('heartbeat', nowSec());
  return { ok: true, fetched: events.length, refreshed: open.length, cursor: maxStart };
}

/** Pull clip + snapshot from Frigate and push to object storage. Idempotent. */
async function uploadArtifacts(sessionId) {
  const events = db.prepare('SELECT * FROM events WHERE session_id = ?').all(sessionId);

  for (const ev of events) {
    for (const kind of ['clip', 'snapshot']) {
      if (kind === 'clip' && !ev.has_clip) continue;
      if (kind === 'snapshot' && !ev.has_snapshot) continue;

      const done = db
        .prepare('SELECT 1 FROM artifacts WHERE event_id = ? AND kind = ?')
        .get(ev.id, kind);
      if (done) continue;

      const buf = kind === 'clip'
        ? await frigate.downloadClip(ev.id)
        : await frigate.downloadSnapshot(ev.id);
      const key = storage.keyFor(ev.id, kind);
      const { bytes } = await storage.upload(
        key, buf, kind === 'clip' ? 'video/mp4' : 'image/jpeg'
      );

      db.prepare(
        'INSERT OR REPLACE INTO artifacts (event_id, kind, s3_key, bytes, uploaded_at) VALUES (?,?,?,?,?)'
      ).run(ev.id, kind, key, bytes, Date.now());
      log.info('uploaded', { event: ev.id, kind, bytes });
    }
  }

  db.prepare('UPDATE sessions SET clips_uploaded = 1, updated_at = ? WHERE id = ?')
    .run(Date.now(), sessionId);
}

/**
 * Publish one closed session. Each sink is tracked separately, so a Notion
 * failure never re-fires a Slack alert that already went out.
 */
async function publishSession(session) {
  const known = JSON.parse(session.people_known || '[]');

  if (!session.clips_uploaded && session.kind !== 'gap') {
    await uploadArtifacts(session.id);
  }

  // Durable link: points at us, we mint a fresh presigned URL on click.
  const firstClip = db
    .prepare(
      `SELECT a.event_id FROM artifacts a
        JOIN events e ON e.id = a.event_id
       WHERE e.session_id = ? AND a.kind = 'clip'
       ORDER BY e.start_time LIMIT 1`
    )
    .get(session.id);
  const clipUrl = firstClip
    ? `${config.server.publicBaseUrl.replace(/\/$/, '')}/clip/${encodeURIComponent(firstClip.event_id)}`
    : null;

  let notionPageId = session.notion_page_id;
  if (!notionPageId) {
    notionPageId = await createNotionRow(session, known, clipUrl);
    db.prepare('UPDATE sessions SET notion_page_id = ?, updated_at = ? WHERE id = ?')
      .run(notionPageId, Date.now(), session.id);
  }

  // Alert only on unknowns. Everything is logged; only exceptions interrupt.
  if (!session.slack_ts && session.count_unknown > 0) {
    const snap = db
      .prepare(
        `SELECT a.s3_key FROM artifacts a
          JOIN events e ON e.id = a.event_id
         WHERE e.session_id = ? AND a.kind = 'snapshot' AND e.sub_label IS NULL
         ORDER BY e.start_time LIMIT 1`
      )
      .get(session.id);

    const snapshotUrl = snap
      ? await storage.presign(snap.s3_key, config.s3.snapshotUrlTtlSeconds)
      : null;

    const ts = await postSlackAlert(session, known, {
      snapshotUrl,
      clipUrl,
      notionUrl: `https://notion.so/${String(notionPageId).replace(/-/g, '')}`,
    });
    if (ts) {
      db.prepare('UPDATE sessions SET slack_ts = ?, updated_at = ? WHERE id = ?')
        .run(ts, Date.now(), session.id);
    }
  }
}

/** Publish everything closed and not yet fully published, with backoff. */
export async function publishPending() {
  const now = Date.now();
  const pending = db
    .prepare(
      `SELECT * FROM sessions
        WHERE closed_at IS NOT NULL
          AND notion_page_id IS NULL
          AND (next_attempt_at IS NULL OR next_attempt_at <= ?)
        ORDER BY opened_at LIMIT 20`
    )
    .all(now);

  for (const s of pending) {
    try {
      await publishSession(s);
      db.prepare('UPDATE sessions SET last_error = NULL, updated_at = ? WHERE id = ?')
        .run(Date.now(), s.id);
      log.info('published', { session: s.id, kind: s.kind });
    } catch (err) {
      const attempts = s.publish_attempts + 1;
      const backoff =
        config.retryBackoff[Math.min(attempts - 1, config.retryBackoff.length - 1)];
      db.prepare(
        `UPDATE sessions SET publish_attempts = ?, next_attempt_at = ?, last_error = ?, updated_at = ?
          WHERE id = ?`
      ).run(attempts, Date.now() + backoff * 1000, err.message, Date.now(), s.id);
      log.error('publish failed', { session: s.id, attempts, retryInSec: backoff, err: err.message });
    }
  }
}

export async function tick() {
  await poll();
  closeDueSessions();
  await publishPending();
}
