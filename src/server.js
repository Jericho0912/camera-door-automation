import express from 'express';
import { randomUUID } from 'node:crypto';
import { db, getMeta, nowSec } from './db.js';
import { config } from './config.js';
import { log } from './log.js';
import { recordUnlock } from './sessions.js';
import { presign } from './storage.js';

export function createServer() {
  const app = express();
  app.use(express.json({ limit: '256kb' }));

  /**
   * Home Assistant posts here when the lock opens. This is a real webhook —
   * HA calls us. (Frigate can't; hence the poller.)
   */
  app.post('/webhook/lock', (req, res) => {
    if (req.get('X-Webhook-Token') !== config.server.lockWebhookToken) {
      return res.sendStatus(401);
    }
    const { id, timestamp, source } = req.body ?? {};
    const ts = timestamp ? Number(timestamp) : nowSec();
    if (!Number.isFinite(ts)) return res.status(400).json({ error: 'bad timestamp' });

    // HA should send a stable id so a retry doesn't create a second session.
    const result = recordUnlock({
      id: id || randomUUID(),
      ts,
      source: source ?? 'home-assistant',
      raw: req.body,
    });
    log.info('unlock received', { ts, ...result });
    res.json({ ok: true, ...result });
  });

  /**
   * Durable clip link. The signature is minted at click time, so a Notion row
   * written in March still resolves in September.
   */
  app.get('/clip/:eventId', async (req, res) => {
    const { eventId } = req.params;
    const row = db
      .prepare(
        `SELECT a.s3_key, e.session_id FROM artifacts a
           JOIN events e ON e.id = a.event_id
          WHERE a.event_id = ? AND a.kind = 'clip'`
      )
      .get(eventId);

    if (!row) {
      const known = db.prepare('SELECT id FROM events WHERE id = ?').get(eventId);
      // 425 Too Early: the poller runs on an interval, so early clicks happen.
      if (known) return res.status(425).send('Clip is still uploading — try again in a minute.');
      return res.status(404).send('No clip for that event.');
    }

    // Every view is attributable. This is the control behind the privacy risk.
    db.prepare(
      'INSERT INTO clip_views (session_id, event_id, viewer, ip, ts) VALUES (?,?,?,?,?)'
    ).run(
      row.session_id,
      eventId,
      // Tailscale can inject identity headers; verify the current names against
      // its docs before relying on them.
      req.get('Tailscale-User-Login') ?? null,
      req.ip,
      Date.now()
    );

    const url = await presign(row.s3_key, config.s3.clipUrlTtlSeconds);
    // Without this a browser can cache the 302 and later replay a dead URL.
    res.set('Cache-Control', 'no-store');
    res.redirect(302, url);
  });

  app.get('/healthz', (_req, res) => {
    const heartbeat = Number(getMeta('heartbeat', 0));
    const ageSec = nowSec() - heartbeat;
    const pending = db
      .prepare('SELECT COUNT(*) n FROM sessions WHERE closed_at IS NOT NULL AND notion_page_id IS NULL')
      .get().n;
    const healthy = ageSec < config.poll.intervalMs / 1000 * 4;
    res.status(healthy ? 200 : 503).json({
      healthy,
      heartbeatAgeSec: Math.round(ageSec),
      cursor: Number(getMeta('cursor.start_time', 0)),
      pendingPublish: pending,
    });
  });

  return app;
}
