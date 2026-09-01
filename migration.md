# Migration Plan

This is a reminder plan only. Do not treat it as an implementation checklist that is already coded.

Goal: move the door-camera automation off personal infrastructure and onto production S3 + production Notion without duplicating pages, losing clips, or deleting local recordings too early.

## Current constraints

- The Mac mini remains the active reconciler host.
- Fregata/Frigate SQLite and recordings remain the source of truth during migration.
- `reconciler-state.db` is important state. Do not delete it.
- Current state tracks uploaded S3 keys and Notion page ids, but it does not yet fully model multiple S3/Notion targets as separate destinations.
- Existing Notion `page_id` values point to the currently configured Notion database. Switching `NOTION_DATABASE_ID` alone can make old events look already synced and skip creating production pages.
- Existing Notion clip links may point to the old bucket until `clips-reset` and a refresh pass regenerate them.
- Local recording cleanup must stay disabled until production S3 and production Notion are verified.

## Phase 0 — Backups before touching production

Run these before changing `.env` on the Mac mini:

```bash
cp .env .env.backup.$(date +%Y%m%d-%H%M%S)
cp reconciler-state.db reconciler-state.db.backup.$(date +%Y%m%d-%H%M%S)
```

Also record current values privately:

- `S3_BUCKET`
- `S3_PREFIX`
- `AWS_REGION`
- `NOTION_DATABASE_ID`
- whether `CLIP_LINKS=true`
- whether `NOTION_INCLUDE_PERSON=true`

## Phase 1 — Production S3 bucket

Create/configure the production bucket first.

Required bucket posture:

- Block Public Access enabled.
- Same prefix convention as current bucket, normally `fregata`.
- Bucket region recorded exactly; `AWS_REGION` must match for presigned links.
- Lifecycle policy optional, but do not expire objects faster than the intended video retention period.

Upload identity policy should be scoped to the production prefix:

- `s3:PutObject`
- `s3:GetObject`
- `s3:AbortMultipartUpload`

Optional dedicated clip-signing identity:

- `s3:GetObject` only on the production prefix.
- Configure as `CLIP_AWS_ACCESS_KEY_ID` / `CLIP_AWS_SECRET_ACCESS_KEY`.
- This gives a kill switch for Notion clip links without disabling uploads.

## Phase 2 — Copy existing objects to production S3

Dry-run first:

```bash
aws s3 sync s3://<personal-bucket>/<prefix>/ s3://<production-bucket>/<prefix>/ --dryrun
```

Apply:

```bash
aws s3 sync s3://<personal-bucket>/<prefix>/ s3://<production-bucket>/<prefix>/
```

Verify object counts/bytes before switching the Mac:

```bash
aws s3 ls s3://<personal-bucket>/<prefix>/ --recursive --summarize
aws s3 ls s3://<production-bucket>/<prefix>/ --recursive --summarize
```

Do not delete personal-bucket objects yet. Keep them until production Notion links are verified.

## Phase 3 — Switch Mac mini to production S3

Update `.env`:

```env
S3_BUCKET=<production-bucket>
S3_PREFIX=fregata
AWS_REGION=<production-bucket-region>
AWS_ACCESS_KEY_ID=<production-upload-key>
AWS_SECRET_ACCESS_KEY=<production-upload-secret>

# optional, recommended for clip links
CLIP_AWS_ACCESS_KEY_ID=<production-readonly-signing-key>
CLIP_AWS_SECRET_ACCESS_KEY=<production-readonly-signing-secret>
```

Then run:

```bash
python3 reconciler.py once
python3 reconciler.py status
```

Expected:

- New uploads land in the production bucket.
- Existing state is not wiped.
- No new `events_failed` or clip signing failures.

## Phase 4 — Production Notion preparation

Create or confirm the production Notion database.

Required properties:

- `Event ID` — title
- `Person` — select
- `Camera` — select
- `Seen` — date
- `Duration (s)` — number
- `Segments` — number
- `Manifest key` — rich text
- `Clip` — URL, if `CLIP_LINKS=true`
- `Score` — number, optional

Access requirements:

- Production Notion integration/token exists.
- Integration is shared with the production database.
- `test-notion.py` passes against production credentials before migration.

Privacy decision:

```env
NOTION_INCLUDE_PERSON=false
```

Keep false unless production Notion is explicitly allowed to store recognized names.

## Phase 5 — Required code support before Notion cutover

Before switching production Notion live, add target-aware Notion migration support.

Needed command:

```bash
python3 reconciler.py notion-migrate --dry-run
python3 reconciler.py notion-migrate --apply
```

Expected command behavior:

- Reads delivered events from `reconciler-state.db`.
- Uses `Event ID` as the stable dedupe key.
- Searches the configured production Notion database for each event.
- If a page exists, records that production `page_id`.
- If missing, creates the production page.
- Does not modify personal Notion pages.
- Does not re-upload S3 objects.
- Is resumable after interruption.
- Rate-limits Notion writes.
- Marks production clip state stale so clip links are regenerated against production S3.

State model should become target-aware, e.g.:

```sql
notion_delivery (
  event_id TEXT,
  notion_database_id TEXT,
  page_id TEXT,
  synced_at REAL,
  last_error TEXT,
  attempts INTEGER NOT NULL DEFAULT 0,
  clip_signed_at REAL,
  clip_attempts INTEGER NOT NULL DEFAULT 0,
  updated_at REAL NOT NULL,
  PRIMARY KEY(event_id, notion_database_id)
)
```

Migration rule:

- Existing rows are assigned to the old/current Notion database id.
- Production rows live beside old rows.
- `sync_notion`, `refresh_clip_links`, Slack Notion links, and `status` must read rows for the currently configured database only.

Do not rely on just changing `NOTION_DATABASE_ID`; that can silently skip historical events.

## Phase 6 — Production Notion dry-run

After code support exists, update `.env` to production Notion values:

```env
NOTION_TOKEN=<production-token>
NOTION_DATABASE_ID=<production-database-id>
```

Run:

```bash
python3 test-notion.py
python3 reconciler.py notion-migrate --dry-run
```

Dry-run should report counts like:

```text
Would create: <n> pages
Would link existing: <n> pages
Would update state only: <n> pages
Would skip: <n> pages
Errors: 0
```

No production writes should happen during dry-run except whatever `test-notion.py` is explicitly designed to test.

## Phase 7 — Apply production Notion migration

Run:

```bash
python3 reconciler.py notion-migrate --apply
python3 reconciler.py clips-reset
python3 reconciler.py once
python3 reconciler.py status
```

Expected:

- Production Notion pages exist for historical delivered events.
- New production page ids are stored in local state.
- Clip links are regenerated with production S3 URLs.
- Personal Notion pages are untouched.
- Slack unknown summaries link to production Notion pages.

Manual verification:

- Open a recent production Notion page.
- Confirm the `Clip` property works.
- Confirm a Slack unknown visitor summary links to production Notion, not personal Notion.
- Confirm no fresh writes are going to the personal S3 bucket.

## Phase 8 — Stabilization window

Wait a few normal operating days before enabling local cleanup.

During this window:

```bash
python3 reconciler.py status
python3 reconciler.py slack-summary <known-test-date>
python3 reconciler.py slack-people-summary <known-test-date>
```

Expected:

- No upload errors.
- No Notion auth/schema errors.
- Clip links continue to refresh.
- Slack summary still shows unknown-only events.
- People summary remains a plain numbered name list.

## Phase 9 — Local recording cleanup, later

Only after production S3 and production Notion are verified, implement local cleanup.

Suggested configuration:

```env
LOCAL_RECORDING_CLEANUP=false
LOCAL_RECORDING_CLEANUP_AFTER_DAYS=7
LOCAL_RECORDING_CLEANUP_DRY_RUN=true
```

Suggested commands:

```bash
python3 reconciler.py cleanup-local --dry-run
python3 reconciler.py cleanup-local --apply
```

Deletion safety rules:

- Delete only files under `FREGATA_RECORDINGS_DIR`.
- Delete only regular files.
- Delete only files older than the retention window.
- Delete only if the matching object exists in the currently configured production bucket.
- Verify S3 `ContentLength` matches local file size before deletion.
- Record deletion state in SQLite.
- Never delete Fregata DB files, config files, face-recognition assets, Notion data, or S3 objects.

Suggested state additions:

```sql
ALTER TABLE segment_delivery ADD COLUMN local_deleted_at REAL;
ALTER TABLE segment_delivery ADD COLUMN local_delete_error TEXT;
```

Bucket-aware hardening to consider:

```sql
ALTER TABLE segment_delivery ADD COLUMN bucket TEXT;
ALTER TABLE segment_delivery ADD COLUMN region TEXT;
ALTER TABLE segment_delivery ADD COLUMN endpoint_url TEXT;
```

Even with bucket-aware state, deletion must still verify against the currently configured production bucket before removing local files.

## Rollback plan

If production S3 fails:

- Restore old `.env` S3 values.
- Run `python3 reconciler.py once`.
- Run `python3 reconciler.py status`.
- Do not delete production objects until root cause is known.

If production Notion fails:

- Restore old `.env` Notion values.
- Run `python3 reconciler.py clips-reset` only if old links need repair.
- Keep production pages already created; do not mass-delete during incident response.
- Fix schema/token/share access, then rerun migration dry-run.

If clip links fail after cutover:

- Check `AWS_REGION` matches the production bucket region.
- Check signing identity has `s3:GetObject` on the production prefix.
- Run `python3 reconciler.py clips-reset` after credentials are fixed.

## Final acceptance criteria

Migration is complete only when:

- New S3 uploads land in production.
- Historical S3 objects needed by Notion links exist in production.
- Production Notion has pages for delivered historical events.
- Production Notion `Clip` links open successfully.
- Slack unknown summaries link to production Notion.
- Personal AWS credentials are no longer used by the Mac mini.
- Personal Notion credentials are no longer used by the Mac mini.
- Local cleanup is still disabled until the stabilization window passes.
