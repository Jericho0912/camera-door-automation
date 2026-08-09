import { randomUUID } from 'node:crypto';
import { db, nowSec } from './db.js';
import { config } from './config.js';
import { log } from './log.js';

const S = config.session;

/** Insert or update an event from Frigate. Safe to replay. */
export function upsertEvent(raw) {
  const now = Date.now();
  const existing = db.prepare('SELECT id, session_id FROM events WHERE id = ?').get(raw.id);

  db.prepare(
    `INSERT INTO events
       (id, camera, label, sub_label, sub_label_score, start_time, end_time,
        has_clip, has_snapshot, raw, first_seen_at, updated_at)
     VALUES (@id, @camera, @label, @sub_label, @sub_label_score, @start_time, @end_time,
             @has_clip, @has_snapshot, @raw, @now, @now)
     ON CONFLICT(id) DO UPDATE SET
       sub_label       = excluded.sub_label,
       sub_label_score = excluded.sub_label_score,
       end_time        = excluded.end_time,
       has_clip        = excluded.has_clip,
       has_snapshot    = excluded.has_snapshot,
       raw             = excluded.raw,
       updated_at      = excluded.updated_at`
  ).run({
    id: raw.id,
    camera: raw.camera,
    label: raw.label,
    // Frigate sets sub_label to the recognised name. NULL/empty => unknown person.
    sub_label: raw.sub_label || null,
    sub_label_score: raw.data?.sub_label_score ?? raw.sub_label_score ?? null,
    start_time: raw.start_time,
    end_time: raw.end_time ?? null,
    has_clip: raw.has_clip ? 1 : 0,
    has_snapshot: raw.has_snapshot ? 1 : 0,
    raw: JSON.stringify(raw),
    now,
  });

  // Attach on first sight rather than waiting for the event to finalise —
  // otherwise a long-running detection could let its session close underneath it.
  if (!existing?.session_id) assignEventToSession(raw);
  return db.prepare('SELECT * FROM events WHERE id = ?').get(raw.id);
}

function assignEventToSession(ev) {
  const open = db
    .prepare(
      `SELECT * FROM sessions
        WHERE closed_at IS NULL AND kind != 'gap'
          AND ? >= opened_at - ?
          AND ? <= window_until
        ORDER BY opened_at DESC LIMIT 1`
    )
    .get(ev.start_time, S.preUnlockLookbackSeconds, ev.start_time);

  if (open) {
    const extended = Math.min(
      Math.max(open.window_until, ev.start_time + S.extendSeconds),
      open.opened_at + S.maxSeconds
    );
    db.prepare('UPDATE sessions SET window_until = ?, updated_at = ? WHERE id = ?')
      .run(extended, Date.now(), open.id);
    db.prepare('UPDATE events SET session_id = ? WHERE id = ?').run(open.id, ev.id);
    return open.id;
  }

  // No unlock preceded this — someone entered without the lock firing, exited,
  // or tailgated after the window closed. Still gets logged.
  const id = randomUUID();
  const now = Date.now();
  db.prepare(
    `INSERT INTO sessions (id, kind, opened_at, window_until, has_unlock, created_at, updated_at)
     VALUES (?, 'entry', ?, ?, 0, ?, ?)`
  ).run(id, ev.start_time, ev.start_time + S.extendSeconds, now, now);
  db.prepare('UPDATE events SET session_id = ? WHERE id = ?').run(id, ev.id);
  log.info('session opened by camera', { session: id, event: ev.id });
  return id;
}

/** Called from POST /webhook/lock. Idempotent on the supplied id. */
export function recordUnlock({ id, ts, source, raw }) {
  const existing = db.prepare('SELECT id FROM unlocks WHERE id = ?').get(id);
  if (existing) return { duplicate: true };

  // People are usually detected walking up to the door before the lock fires,
  // so look both directions.
  let session = db
    .prepare(
      `SELECT * FROM sessions
        WHERE closed_at IS NULL AND kind != 'gap'
          AND opened_at <= ? + ?
          AND window_until >= ? - ?
        ORDER BY opened_at DESC LIMIT 1`
    )
    .get(ts, S.preUnlockLookbackSeconds, ts, S.preUnlockLookbackSeconds);

  const now = Date.now();
  if (session) {
    const extended = Math.min(
      Math.max(session.window_until, ts + S.windowSeconds),
      session.opened_at + S.maxSeconds
    );
    db.prepare(
      'UPDATE sessions SET has_unlock = 1, window_until = ?, updated_at = ? WHERE id = ?'
    ).run(extended, now, session.id);
  } else {
    const sid = randomUUID();
    db.prepare(
      `INSERT INTO sessions (id, kind, opened_at, window_until, has_unlock, created_at, updated_at)
       VALUES (?, 'entry', ?, ?, 1, ?, ?)`
    ).run(sid, ts, ts + S.windowSeconds, now, now);
    session = { id: sid };
  }

  db.prepare(
    'INSERT INTO unlocks (id, ts, source, session_id, raw, created_at) VALUES (?,?,?,?,?,?)'
  ).run(id, ts, source ?? null, session.id, raw ? JSON.stringify(raw) : null, now);

  return { duplicate: false, sessionId: session.id };
}

function recomputeCounts(sessionId) {
  const events = db
    .prepare('SELECT sub_label FROM events WHERE session_id = ?')
    .all(sessionId);

  const known = [...new Set(events.filter((e) => e.sub_label).map((e) => e.sub_label))];
  // NOTE: unknowns are counted per event, not per person. If tracking drops and
  // re-acquires the same stranger, that reads as 2. Tune during soak; the
  // review queue lets an admin correct it.
  const unknown = events.filter((e) => !e.sub_label).length;

  db.prepare(
    `UPDATE sessions SET people_known = ?, count_total = ?, count_unknown = ?, updated_at = ?
      WHERE id = ?`
  ).run(JSON.stringify(known), known.length + unknown, unknown, Date.now(), sessionId);

  return { known, unknown, total: known.length + unknown };
}

/** Close sessions whose window has expired and whose events have all finalised. */
export function closeDueSessions() {
  const now = nowSec();
  const due = db
    .prepare(
      `SELECT * FROM sessions
        WHERE closed_at IS NULL AND kind != 'gap' AND window_until + ? < ?`
    )
    .all(S.closeGraceSeconds, now);

  const closed = [];
  for (const s of due) {
    const inProgress = db
      .prepare('SELECT COUNT(*) n FROM events WHERE session_id = ? AND end_time IS NULL')
      .get(s.id).n;

    // Don't wait forever on a detection that never ends.
    const overdue = s.opened_at + S.maxSeconds + S.closeGraceSeconds * 2 < now;
    if (inProgress > 0 && !overdue) continue;
    if (inProgress > 0) log.warn('closing session with in-progress events', { session: s.id, inProgress });

    const counts = recomputeCounts(s.id);
    // An unlock fired but nobody was on camera: door tested, opened from inside,
    // or the camera missed it. Worth a row either way.
    const kind = counts.total === 0 && s.has_unlock ? 'unlock_no_camera' : 'entry';

    db.prepare('UPDATE sessions SET closed_at = ?, kind = ?, updated_at = ? WHERE id = ?')
      .run(now, kind, Date.now(), s.id);

    closed.push({ ...s, ...counts, kind });
    log.info('session closed', { session: s.id, kind, total: counts.total, unknown: counts.unknown });
  }
  return closed;
}

/** A visible record that we weren't watching. Written on boot after downtime. */
export function writeGapSession(fromSec, toSec) {
  const id = randomUUID();
  const now = Date.now();
  db.prepare(
    `INSERT INTO sessions
       (id, kind, opened_at, window_until, closed_at, gap_from, gap_to,
        count_total, count_unknown, has_unlock, created_at, updated_at)
     VALUES (?, 'gap', ?, ?, ?, ?, ?, 0, 0, 0, ?, ?)`
  ).run(id, fromSec, toSec, toSec, fromSec, toSec, now, now);
  log.warn('gap recorded', { from: fromSec, to: toSec, minutes: Math.round((toSec - fromSec) / 60) });
  return id;
}
