# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Commands

```bash
npm install
cp .env.example .env       # fill in credentials; config.js throws at import on missing required vars
mkdir -p data logs
npm start                  # node src/index.js
npm run dev                # node --watch
npm run check              # node --check (syntax only — this is not a test suite)
```

Node 20+ required (uses native `fetch`, `AbortSignal.timeout`).

There is no test runner, linter, or CI. Verification is manual, via the "Exit tests" in README.md — walk the door, stop/restart the service, then inspect SQLite:

```bash
sqlite3 data/entry-logger.db "SELECT id, COUNT(*) FROM events GROUP BY id HAVING COUNT(*) > 1;"
sqlite3 data/entry-logger.db "SELECT id, kind, count_total, count_unknown, has_unlock, notion_page_id, last_error FROM sessions ORDER BY opened_at DESC LIMIT 20;"
curl localhost:8787/healthz
```

## Layout

All service code lives in `src/`; `launchd/` holds the plist; `.env.example` is the credential template. `package.json`, `README.md`, and the plist all reference these paths — keep them in sync if anything moves.

`.agents/skills/` holds skills installed from [mattpocock/skills](https://github.com/mattpocock/skills), pinned by `skills-lock.json`; `.claude/skills/` symlinks into it. Update with `npx skills@latest add mattpocock/skills`.

## Architecture

Single Node process, three concurrent concerns, all state in one SQLite file (WAL). No queue, no external scheduler.

1. **Poller** (`pipeline.js` `tick()`, on `POLL_INTERVAL_MS`) — `poll()` → `closeDueSessions()` → `publishPending()`. `index.js` guards against overlapping ticks with a `running` flag.
2. **HTTP server** (`server.js`) — `POST /webhook/lock` (HA pushes unlocks), `GET /clip/:eventId`, `GET /healthz`.
3. **Boot gap detection** (`index.js`) — a stale `heartbeat` meta key means the service was down; writes a `kind='gap'` session row.

Data flow: Frigate events → `events` table → grouped into `sessions` → clip/snapshot to S3 (`artifacts`) → one Notion page per session → Slack only when `count_unknown > 0`.

### Design invariants — preserve these when changing anything

- **Pull, not push.** Frigate has no webhook. The cursor (`meta['cursor.start_time']`) advances only after a successful write, so a crash replays rather than skips. Each pass re-reads `POLL_OVERLAP_SECONDS` behind the cursor because Frigate can insert events with an older `start_time` than one already seen — safe only because every write is upsert-by-id. Don't "optimize" the overlap away or make writes non-idempotent.
- **Cursor is on `start_time`, not `end_time`,** because that's what Frigate's `after` filter uses. Events that hadn't ended are re-fetched separately each pass (they may gain a `sub_label` face match on close).
- **Session windowing** lives entirely in `sessions.js`. An unlock opens `[ts - PRE_UNLOCK_LOOKBACK, ts + SESSION_WINDOW]`; each attached detection extends it by `SESSION_EXTEND_SECONDS`, hard-capped at `opened_at + SESSION_MAX_SECONDS` (that cap is what makes tailgating one session instead of an infinite one). Events attach on **first sight**, not on finalise, so a long detection can't have its session close underneath it.
- **Known vs unknown comes from `sub_label`**: a name means recognised, NULL means unknown. Unknowns are counted per *event*, not per person — a dropped-and-reacquired stranger reads as 2. Known limitation; don't add dedup without soak data.
- **Clip links are indirect on purpose.** Notion stores `PUBLIC_BASE_URL/clip/:eventId`; the presigned S3 URL is minted at click time in `server.js`, so rows written months ago still resolve. Every hit is recorded in `clip_views` — that audit trail is the privacy control, not incidental logging. Never write a presigned URL into Notion.
- **Sinks are tracked separately** (`notion_page_id`, `slack_ts`, `clips_uploaded`) so a failure in one never re-fires another.

### Publish loop gotcha

`publishPending()` selects `WHERE closed_at IS NOT NULL AND notion_page_id IS NULL`. Once the Notion page exists the session is never re-selected — so a Slack alert that fails *after* Notion succeeded is dropped, not retried. Backoff (`config.retryBackoff`, indexed by `publish_attempts`) therefore only covers the pre-Notion path. Keep this in mind before assuming a sink self-heals.

## Conventions

- ESM throughout (`"type": "module"`), explicit `.js` extensions on relative imports.
- `better-sqlite3` is synchronous — DB calls are plain, not awaited. Multi-row writes use `db.transaction()`.
- Logging is one JSON object per line via `log.js` (`log.info/warn/error(msg, meta)`). No logger library; launchd captures stdout/stderr to `logs/`.
- All tunables go through `config.js` — `req()` for required, `num()` for optional-with-default. Never read `process.env` outside that file.
- Timestamps: **seconds** (floats, Frigate's convention) for anything event- or session-related; **milliseconds** (`Date.now()`) for `created_at`/`updated_at`/`next_attempt_at`. `nowSec()` in `db.js` is the seconds clock. Mixing these is the easiest bug to introduce here.
- Schema is `CREATE TABLE IF NOT EXISTS` executed at import in `db.js`. There are no migrations — changing an existing column means writing the migration path yourself.
- Notion property names (`Name`, `Date`, `People entered`, `Unknown`, `Video recording`, `Status`) appear only in `sinks.js` and must match the database exactly.

## Deployment

Mac Mini under launchd (`com.swarm.entry-logger.plist`, `RunAtLoad` + `KeepAlive`). LaunchAgents need a logged-in session, so auto-login must be enabled or nothing restarts after a power cut. `PUBLIC_BASE_URL` is a Tailscale hostname; Slack image URLs must be publicly reachable presigned S3 links instead, since Slack's servers fetch them.
