import 'dotenv/config';

function req(name) {
  const v = process.env[name];
  if (!v) throw new Error(`Missing required env var: ${name}`);
  return v;
}
const num = (name, dflt) => (process.env[name] ? Number(process.env[name]) : dflt);

export const config = {
  // --- Frigate / Fregata ---
  frigate: {
    baseUrl: process.env.FRIGATE_URL || 'http://127.0.0.1:5000',
    camera: process.env.FRIGATE_CAMERA || 'door',
    label: 'person',
  },

  // --- Poller ---
  poll: {
    intervalMs: num('POLL_INTERVAL_MS', 30_000),
    // Re-scan this far behind the cursor each pass. Frigate can insert an event
    // whose start_time is slightly older than one we've already seen, so a strict
    // high-water mark would skip it. Overlap + upsert-by-id makes that safe.
    overlapSeconds: num('POLL_OVERLAP_SECONDS', 120),
    pageSize: num('POLL_PAGE_SIZE', 100),
  },

  // --- Session windowing ---
  // An unlock opens a window; person events inside it belong to that session.
  session: {
    // People are usually detected walking up to the door BEFORE the lock fires.
    preUnlockLookbackSeconds: num('PRE_UNLOCK_LOOKBACK_SECONDS', 20),
    // How long after an unlock we keep accepting detections into the session.
    windowSeconds: num('SESSION_WINDOW_SECONDS', 90),
    // Each new detection pushes the window out by this much (tailgating).
    extendSeconds: num('SESSION_EXTEND_SECONDS', 30),
    // Hard cap so a busy doorway can't produce one infinite session.
    maxSeconds: num('SESSION_MAX_SECONDS', 300),
    // Wait this long past window_until before closing, so events that finalise
    // late still land in the right session.
    closeGraceSeconds: num('SESSION_CLOSE_GRACE_SECONDS', 60),
  },

  // --- Storage (S3 / R2 / B2) ---
  s3: {
    bucket: req('S3_BUCKET'),
    region: process.env.S3_REGION || 'auto',
    endpoint: process.env.S3_ENDPOINT || undefined, // set for R2/B2
    accessKeyId: req('S3_ACCESS_KEY_ID'),
    secretAccessKey: req('S3_SECRET_ACCESS_KEY'),
    forcePathStyle: process.env.S3_FORCE_PATH_STYLE === 'true',
    // Short: the browser follows the 302 immediately.
    clipUrlTtlSeconds: num('CLIP_URL_TTL_SECONDS', 300),
    // Longer: Slack's servers fetch this, possibly after a delay.
    snapshotUrlTtlSeconds: num('SNAPSHOT_URL_TTL_SECONDS', 86_400),
  },

  // --- Notion ---
  notion: {
    token: req('NOTION_TOKEN'),
    databaseId: req('NOTION_DATABASE_ID'),
  },

  // --- Slack ---
  slack: {
    botToken: process.env.SLACK_BOT_TOKEN || null,
    channel: process.env.SLACK_CHANNEL || '#door-log',
    enabled: !!process.env.SLACK_BOT_TOKEN,
  },

  // --- HTTP server ---
  server: {
    port: num('PORT', 8787),
    // Shared secret for POST /webhook/lock. HA sends it as X-Webhook-Token.
    lockWebhookToken: req('LOCK_WEBHOOK_TOKEN'),
    // Public base for durable clip links, e.g. https://mac-mini.tailnet.ts.net
    publicBaseUrl: req('PUBLIC_BASE_URL'),
  },

  // --- Local state ---
  dbPath: process.env.DB_PATH || './data/entry-logger.db',

  // If the cursor is staler than this at boot, we were down: write a gap row.
  gapThresholdSeconds: num('GAP_THRESHOLD_SECONDS', 600),

  // Publish retry backoff, in seconds, indexed by attempt count.
  retryBackoff: [10, 30, 120, 600, 1800, 3600],
};
