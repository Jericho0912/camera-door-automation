# Production Migration — Access Request & Runbook

**Who this is for:** the infra lead migrating the door-camera archiver to production.
**What to do with it:** Section 2 is the access request — copy it and send it as-is (fill
in the one placeholder). Section 3 justifies every permission in that request by pointing
at the exact function that makes the API call, so the request can be audited against the
code. Sections 4–5 are the migration runbook. Section 6 lists the decisions only the lead
can make.

**Least-privilege by design:** every permission below maps to an API call the code
actually makes — nothing speculative, nothing "just in case." The tool cannot delete,
cannot list, and cannot reach any key outside its own prefix. That is deliberate, and the
rollback model in Section 5.6 depends on it staying that way.

---

## 1. The system in one paragraph

`reconciler.py` polls a Fregata NVR's SQLite database (via a read-only snapshot) for
finalized `person` events on `door_camera`, uploads the overlapping video segments to S3,
optionally uploads a per-event manifest JSON, and creates one page per event in a Notion
database. `clipserver.py` serves permanent `https://<host>/clip/<event_id>` links
(written into Notion's Clip column) that resolve, at click time, to 5-minute presigned S3
URLs. It binds to loopback and sits behind `tailscale serve`, so the tailnet is the
access control and the Notion link is worthless off it. All delivery state lives in a
local SQLite state DB; every operation is idempotent and safe to interrupt and re-run.

Verified scale at time of writing: 312 finalized events, 1,891 distinct segment files,
21 GB in the backlog, growing ~53 GB/month.

---

## 2. The access request (copy-paste this)

> **Subject: Access for the door-camera archiver (production)**
>
> I need two credentials for the door-camera → S3 → Notion archiver, plus three settings
> on the production bucket.
>
> **1) AWS — a dedicated IAM user, programmatic access key only (no console access),
> with exactly this policy:**
>
> ```json
> {
>   "Version": "2012-10-17",
>   "Statement": [{
>     "Sid": "DoorCameraArchiverWriteAndVerify",
>     "Effect": "Allow",
>     "Action": [
>       "s3:PutObject",
>       "s3:GetObject",
>       "s3:AbortMultipartUpload"
>     ],
>     "Resource": "arn:aws:s3:::<PROD-BUCKET>/fregata/*"
>   }]
> }
> ```
>
> Replace `<PROD-BUCKET>` with the production bucket name. `fregata/` is the key prefix
> the tool writes under (its `S3_PREFIX` setting) — if you want a different prefix, tell
> me and I'll set the config to match; the policy resource and the config must agree.
>
> Please do **not** add `s3:DeleteObject`, `s3:ListBucket`, any bucket-level ARN, or
> console access. The tool is designed to be physically unable to delete or enumerate
> anything, and its rollback/safety model depends on that. If the policy comes back with
> extra permissions "for convenience," I'll ask for them to be removed.
>
> **2) From whoever owns the bucket (bucket configuration, not IAM):**
>
> - **Block Public Access: ON** (all four settings). Nothing in the code sets ACLs or
>   makes objects public; the only exposure path would be bucket configuration.
> - **Versioning: OFF.** Segments shared between overlapping events are uploaded twice
>   under the same key — harmless without versioning, silent storage growth with it.
> - **Lifecycle rule:** expire objects under the `fregata/` prefix after 30–90 days.
>   Nothing in the code deletes, and growth is ~53 GB/month, so this rule is the only
>   thing standing between us and an unbounded bill.
> - Tell me the bucket's **region** (it goes in `AWS_REGION`). Any region works — the
>   client pins Signature Version 4, including regions like ap-southeast-1 that reject
>   SigV2.
> - Tell me whether the bucket enforces **SSE-KMS default encryption**. If it does, the
>   IAM user additionally needs `kms:GenerateDataKey` and `kms:Decrypt` on that CMK (the
>   code sets no encryption headers, so SSE-S3 is transparent but SSE-KMS is not).
>
> **3) Notion — an *internal* integration secret** (Notion → Settings → Connections →
> Develop or manage integrations → New integration, workspace-internal), with
> capabilities:
>
> - Read content
> - Insert content
> - Update content
> - **No** user-information capability (the tool never reads users).
>
> After creating it, the integration must **also be connected to the target database**:
> open the database page → `•••` menu → Connections → Add connection → pick the
> integration. That step is manual, is not an API call, and is easy to miss — a token
> without it gets 404s on every request and looks exactly like a wrong database ID.

---

## 3. Why each permission — traced to the code

Every claim below cites the function that makes the call. Nothing else in `reconciler.py`
or `clipserver.py` touches AWS or Notion.

### 3.1 S3 — the complete list of calls the code makes

| IAM action | Code path | What it does |
|---|---|---|
| `s3:PutObject` | `upload_file()` in `reconciler.py` → `client.upload_file(...)` | Uploads each video segment. boto3's managed transfer sends a single `PutObject` for files under 8 MB and `CreateMultipartUpload` / `UploadPart` / `CompleteMultipartUpload` for larger files — **all of those S3 operations are authorized by `s3:PutObject`**. |
| `s3:PutObject` | `upload_manifest()` in `reconciler.py` → `client.put_object(...)` | Uploads the per-event `manifest.json` (only when `UPLOAD_EVENT_MANIFEST=true`; it stays false for now — see Section 4). |
| `s3:AbortMultipartUpload` | Failure path of the managed transfer in `upload_file()` | When a multipart upload dies partway, boto3 aborts it. Without this permission the abort fails too, and orphaned parts sit invisibly in the bucket, billed, forever (the lifecycle rule's incomplete-multipart setting is the backstop). |
| `s3:GetObject` | `upload_file()` in `reconciler.py` → `client.head_object(...)` | Immediately after **every** upload the code calls `head_object` to fetch the ETag it records in the state DB. `HeadObject` is authorized by `s3:GetObject`. **This is the load-bearing one — see the write-only trap below.** |
| `s3:GetObject` | `presign()` in `clipserver.py` → `client.generate_presigned_url("get_object", ...)` | Every clip link in Notion 302-redirects to a presigned GET. A presigned URL is authorized **as the signing principal, at click time** — so when anyone on the tailnet clicks a clip, it is this IAM user's `s3:GetObject` that S3 evaluates. Remove it and every clip link in Notion breaks. |

**The write-only trap.** It is tempting to grant an "archiver" write-only access. Here
that produces the worst failure mode in the system: `upload_file()` has no try/except
between the upload and the `head_object` verification, so with write-only credentials
**every upload succeeds and then throws**. The segment row is never written to the state
DB, so the next poll (default every 30 s) re-uploads the same file, and the one after
that, forever — every PUT billed, while `status` shows a permanently failing backlog.
`s3:GetObject` is not optional.

**Why the resource is `<PROD-BUCKET>/fregata/*` and not the bucket.** Every key the code
writes is built in `process_event()` as `{S3_PREFIX}/recordings/...` or
`{S3_PREFIX}/events/<camera>/<event_id>/manifest.json`, and every key it reads back or
presigns comes from its own state DB — always under the same prefix. Object-level ARN
only; there is no `ListBucket` because **nothing lists** — every access is by exact key.
(Corollary: keep `S3_PREFIX` non-empty and matching the policy. An empty prefix would put
keys at the bucket root, outside the granted resource.)

**Explicitly NOT requested — do not "helpfully" add these:**

- **`s3:DeleteObject`** — deliberately excluded. The rollback design (Section 5.6, and
  LEARNING-NOTES §6) depends on the reconciler being *physically unable* to delete
  footage: cleanup of a botched first pass is a deliberate human action in the console,
  and no bug or misconfiguration in this code can take evidence away. Retention is the
  lifecycle rule's job, not the tool's.
- **`s3:ListBucket`** — no code path enumerates the bucket.
- **Console access** — nothing needs it; the access key is the whole interface.

**Signature version.** `s3_client()` in `reconciler.py` pins
`BotoConfig(signature_version="s3v4")`. Without the pin, *presigning* can silently fall
back to deprecated SigV2, which newer regions (ap-southeast-1 included) reject outright —
uploads would keep working while every clip link 403s. With the pin, any region works.

**KMS caveat.** The code sets no `ServerSideEncryption` headers anywhere. On a bucket
with SSE-S3 default encryption this is fully transparent. On a bucket that enforces
**SSE-KMS**, S3 encrypts and decrypts with the CMK on the caller's behalf, so the IAM
user additionally needs `kms:GenerateDataKey` (uploads) and `kms:Decrypt` (`head_object`
and every presigned GET) on that key. This is the "which encryption?" question in the
access request — get the answer before the preflight, not after it 403s.

### 3.2 Notion — the complete list of calls the code makes

All Notion traffic goes through `notion_request()` in `reconciler.py`, which sends only
`Authorization: Bearer <token>` and `Notion-Version` headers and handles 429/Retry-After
itself.

| API call | Code path | Capability it needs |
|---|---|---|
| `GET /v1/databases/{id}` | `notion_targets()` in `reconciler.py` — resolves the database's data-source ID once per process (the 2025-09-03 API split moved page parents and querying onto data sources; the code detects which shape the workspace speaks and falls back automatically) | Read content |
| `POST /v1/data_sources/{id}/query` (or `POST /v1/databases/{id}/query` on the pre-split shape) | `sync_notion()` and `backfill_clip()` in `reconciler.py` — dedupe/recovery lookup by the `Event ID` title, so a crash mid-sync cannot produce duplicate pages and a lost `page_id` can be re-found | Read content |
| `POST /v1/pages` | `sync_notion()` in `reconciler.py` — creates one page per event, properties built by `notion_properties()` | Insert content |
| `PATCH /v1/pages/{page_id}` | `backfill_clip()` in `reconciler.py` (new on this branch) — writes the `Clip` URL onto pages that were created before `PUBLIC_BASE_URL` was configured, so the existing backlog gets clickable links without recreating pages | Update content |

**No user-information capability.** No code path reads users, mentions people, or touches
comments. If the integration setup screen asks, the answer is "No user information."

**The manual sharing step, again, because it is the #1 failure.** An internal integration
token is inert until the target database is *connected* to it (database page → `•••` →
Connections → Add connection). Notion deliberately returns **404, not 403**, for
resources a token cannot see — so a missed connection is indistinguishable from a wrong
`NOTION_DATABASE_ID`. The code is defensive about this (`notion_targets()` failures are
logged and *not* charged against any event's retry budget), but it cannot fix it.

**API version.** `NOTION_VERSION` is set in `.env` (currently `2026-03-11`). Because
`notion_targets()` auto-detects the data-source vs. database shape, there is no special
version requirement to put in the access request.

### 3.3 Notion database schema

The target database must have exactly these properties (names are case-sensitive and
matched exactly by `notion_properties()`):

| Property | Type | Notes |
|---|---|---|
| `Event ID` | Title | Also the dedupe key queried by `sync_notion()` / `backfill_clip()` |
| `Person` | Select | Writes `Unrecognized` unless `NOTION_INCLUDE_PERSON=true` (Section 4) |
| `Camera` | Select | |
| `Seen` | Date | Start and end, in the Mac's local timezone (UTC+8) |
| `Duration (s)` | Number | |
| `Segments` | Number | |
| `Manifest key` | Rich text | Empty while manifests are disabled |
| `Score` | Number | Only written when the event has one |
| `Clip` | **URL** | **Create this one manually in Notion — see below.** |

Bootstrap: import `notion-database-template.csv` from the repo to create everything
except `Clip` with the right types, plus a few sample rows. Then add `Clip` by hand
(**+** → property type **URL** → name it exactly `Clip`) — CSV import cannot reliably set
a URL-typed property, which is why it is not in the template. Delete the sample rows
after import.

Ordering matters: a database with no `Clip` property 400s every `PATCH`, and
`backfill_clip()` charges those 400s against each event's `NOTION_MAX_ATTEMPTS` budget
(a 4xx means "this page will never accept it"). Create the property **before** setting
`PUBLIC_BASE_URL`, or the backlog burns its retry budget and goes terminal.

---

## 4. Production `.env` checklist

`.env.example` in the repo documents every variable the code reads; copy it and fill in.
The ones that matter for the migration, and the two that need an explicit decision:

| Variable | Production value | Why |
|---|---|---|
| `FREGATA_DB_PATH`, `FREGATA_RECORDINGS_DIR` | Absolute paths on the Mac Mini | `FREGATA_RECORDINGS_DIR` must be the exact prefix of the `recordings.path` column values, or keys land under hashed `external/` names — see the rollback trap (5.6) |
| `STATE_DB_PATH` | **Absolute** file path | The default is relative to the working directory; launching from elsewhere creates a second empty state DB and re-uploads everything |
| `CAMERA`, `LABEL` | `door_camera`, `person` | Exact string match against the NVR DB |
| `S3_BUCKET` | `<PROD-BUCKET>` | |
| `S3_PREFIX` | `fregata` (or whatever the IAM policy resource says) | Must match the policy ARN; must be non-empty |
| `AWS_REGION` | The bucket's region | From the bucket owner; SigV4 is pinned so any region works |
| `AWS_ACCESS_KEY_ID`, `AWS_SECRET_ACCESS_KEY` | The new IAM user's key | Read by boto3 itself, hence the `AWS_` names. Do **not** set `S3_ENDPOINT_URL` for AWS — leave the line out entirely |
| `NOTION_TOKEN` | The internal integration secret | |
| `NOTION_DATABASE_ID` | The 32 hex chars from the database URL, before the `?` | |
| `NOTION_VERSION` | `2026-03-11` (as in `.env.example`) | |
| `NOTION_INCLUDE_PERSON` | `false` until signed off | **Privacy sign-off item for the lead.** `sub_label` is a real person's name from face recognition. With `false`, every page reads "Unrecognized." Setting `true` publishes household members' names to Notion — that is a decision, not a default. |
| `NOTION_MAX_ATTEMPTS` | `5` (default) | Per-event retry budget for page creation and, separately, for the Clip backfill; only the event's own 4xx failures are charged against it |
| `DRY_RUN` | `true` for the rehearsal, `false` at go-live | Accepts only `1/true/yes/on` as true — any typo means LIVE. It fails toward uploading. |
| `UPLOAD_EVENT_MANIFEST` | **`false` — must stay false** | The manifest embeds the entire event row **including `sub_label`, a real person's name**, and manifests are fetchable through clip links (`/manifest/<event_id>`). Stays false until the manifest fields are filtered (tracked in LEARNING-NOTES' deferred backlog). Not a lead decision — a prerequisite. ⚠️ The code's default when the line is **absent** is `true` — keep the line present and set to `false`; deleting it silently turns manifests on. |
| `POLL_SECONDS`, `SETTLE_SECONDS` | `30`, `5` (defaults) | |
| `PUBLIC_BASE_URL` | `https://<mac-mini>.<tailnet>.ts.net` | The stable base written into the `Clip` column. Leave blank until clipserver bring-up (5.7); blank means no Clip column is written and no backfill runs. Setting it later backfills existing pages via PATCH. |
| `CLIP_URL_TTL_SECONDS` | `300` (default) | Lifetime of each presigned URL after a click |
| `CLIP_SERVER_PORT` | `8787` (default) | Loopback only; `tailscale serve` fronts it |

Secrets hygiene: `chmod 600 .env`; confirm `git check-ignore -v .env` prints a rule; and
confirm `git status --porcelain` does not show `.env`.

---

## 5. Migration runbook

Run everything as the user that will own the launchd job, from the repo directory, venv
active. The order matters: each step is a precondition for the next.

### 5.1 Credential preflight (before pushing 21 GB)

This exercises **every** S3 call the system makes — plain PUT, multipart PUT, HeadObject,
and a presigned GET — for the cost of two small objects, and gives each failure its own
distinct error. Adapted from LEARNING-NOTES §7, with two additions: the `s3v4` pin (so
the preflight signs exactly like production `s3_client()`) and the presign check (so clip
links are proven before Notion ever holds one).

```bash
dd if=/dev/zero of=/tmp/preflight-10m.bin bs=1m count=10   # >8 MiB forces multipart

python3 - <<'PY'
import os, boto3
from botocore.config import Config
from dotenv import load_dotenv
load_dotenv()
c = boto3.client('s3',
    region_name=os.getenv('AWS_REGION', 'us-east-1'),
    endpoint_url=os.getenv('S3_ENDPOINT_URL') or None,
    config=Config(signature_version='s3v4',
                  retries={'max_attempts': 8, 'mode': 'standard'}))
b = os.environ['S3_BUCKET']
p = os.getenv('S3_PREFIX', 'fregata').strip('/')
k1, k2 = f'{p}/_preflight.txt', f'{p}/_preflight-10m.bin'
c.put_object(Bucket=b, Key=k1, Body=b'ok', ContentType='text/plain')
print('PutObject OK', k1)
print('HeadObject OK', c.head_object(Bucket=b, Key=k1)['ETag'])
c.upload_file('/tmp/preflight-10m.bin', b, k2,
              ExtraArgs={'ContentType': 'application/octet-stream'})
print('Multipart upload_file OK', k2)
print('HeadObject OK', c.head_object(Bucket=b, Key=k2)['ETag'])
url = c.generate_presigned_url('get_object', Params={'Bucket': b, 'Key': k1}, ExpiresIn=120)
print('Presign OK')
print(url)
PY
```

All five `OK` lines must print, then prove the presigned GET actually works:

```bash
curl -sf "<the printed URL>" && echo "Presigned GET OK"
```

- **`PutObject` succeeds but `HeadObject` 403s: STOP.** That is the missing
  `s3:GetObject` failure from Section 3.1, and going live like this re-uploads forever.
- **Presigned GET 403s** while HeadObject worked: check the signature version (a region
  rejecting SigV2) or the KMS caveat (`kms:Decrypt` missing).
- Afterwards, ask someone with console access to delete the two `_preflight` objects —
  the minimum policy deliberately cannot.

### 5.2 Dry-run rehearsal

With `DRY_RUN=true` and the production `.env` otherwise complete:

```bash
python3 reconciler.py once 2>&1 | tee dryrun.log
echo "exit=${pipestatus[1]}"            # zsh; tee hides the real exit code

grep -c Traceback dryrun.log            # must be 0
grep -c 'external/' dryrun.log          # must be 0 — mangled keys (see 5.6 for why)
grep -c 'DRY RUN upload /' dryrun.log   # the planned upload count
```

Any `external/` key means `FREGATA_RECORDINGS_DIR` does not match the `recordings.path`
prefix — fix it **now**, while it costs nothing. The last verified clean run planned 312
events / 1,891 distinct files / 21 GB; expect the same shape, larger.

Note: `DRY_RUN=true` also disables the Notion sink entirely (`sync_notion()` returns
before doing anything), so the dry run validates S3 planning only. Notion failures are
tolerable at go-live — retries dedupe by `Event ID` before creating, so a duplicate page
requires a second failure in the dedupe lookup itself: rare, and harmless beyond a
duplicate row. But **tolerable is not the same as untested**: a mistyped property name or
type in the Notion database 400s every page creation, and a non-429 4xx is charged
against the event, so the entire 312-event backlog would burn its `NOTION_MAX_ATTEMPTS`
budget and go terminal before anyone noticed. Preflight the schema first.

### 5.2b Notion preflight

With the prod `NOTION_TOKEN` and `NOTION_DATABASE_ID` in `.env`, run the repo's
read-only schema check and require every property to print `ok` before going live:

```bash
python3 test-notion.py
```

It verifies the token, the database share, and each property name and type the sink
writes. `Clip` (type URL) joins the checklist once `PUBLIC_BASE_URL` is set — so re-run
it at clipserver bring-up (5.7), where a missing `Clip` property would otherwise burn
the whole backlog's PATCH budget. This is the "dry run → S3 preflight → **Notion
preflight** → live" sequence LEARNING-NOTES §6 documents.

### 5.3 Go live

1. Set `DRY_RUN=false`.
2. `python3 reconciler.py once` — backfills the entire backlog (21+ GB). At 10 Mbps up
   that is roughly 5 hours; at 50 Mbps roughly 1 hour. Start it when the bandwidth is
   not needed. It is safe to interrupt: every completed segment is recorded in the state
   DB, and the next run resumes where it stopped.
3. `python3 reconciler.py status` — expect `events_complete` ≈ events found,
   `notion_synced` climbing to match, and empty failure lists.

### 5.4 Convergence test

```bash
python3 reconciler.py once   # again, immediately
```

Assert **both**: it finds the **same N** events, and it completes **~zero** of them (all
short-circuit as already delivered). Zero completions alone can mean "converged" or
"found nothing" — very different things, which is why both halves are required.

### 5.5 Integrity spot-check

Pick one segment and compare S3's `ContentLength` (from a `head_object`) against the
local file's `stat -f%z` size — then download one clip **and actually play it**. A key
listing proves a name exists; it passes just as happily for a truncated file.

### 5.6 The rollback trap — read before you need it

If the first live pass lands objects under `external/` keys (bad
`FREGATA_RECORDINGS_DIR`), **fixing the config and re-running does nothing**: those
events are marked complete in the state DB and short-circuit forever. Clearing
`completed_at` alone does not help either — the segment rows survive with their old keys
and ETags, so the uploads are skipped, and (if manifests were enabled) a fresh manifest
would point at newly computed keys that hold no objects. The only clean reset is
deleting the state DB — the file `STATE_DB_PATH` points at, plus its WAL siblings:

```bash
rm "$STATE_DB_PATH" "$STATE_DB_PATH-wal" "$STATE_DB_PATH-shm"
```

— which means paying for the full backfill again, and the orphaned objects must be
deleted from the console (the IAM policy cannot). This is exactly why 5.2's
`grep -c 'external/'` must be zero before `DRY_RUN=false`, and why the policy excludes
`s3:DeleteObject`: a bad pass wastes money, but no configuration mistake can destroy
footage.

The same one-way logic applies to Notion: an event whose `synced_at` (or
`clip_synced_at`) is set is never re-synced, and an event that exhausted its
`NOTION_MAX_ATTEMPTS` budget is terminal until its `notion_delivery` row is cleared by
hand.

### 5.7 Clipserver bring-up

Do this after S3 convergence, so links have something to resolve to. The order below is
load-bearing: the `Clip` property must exist before `PUBLIC_BASE_URL` is set, or the
backfill's PATCHes 400 and burn each event's retry budget (Section 3.3).

1. In Notion, confirm the `Clip` property exists with type **URL** (Section 3.3).
2. Get the Mac's tailnet name: `tailscale status` — e.g. `mac-mini.tailXXXX.ts.net`.
3. Set `PUBLIC_BASE_URL=https://mac-mini.tailXXXX.ts.net` in `.env` (a trailing slash is
   tolerated; the code strips it).
4. Start the server: `python3 clipserver.py` — it binds **127.0.0.1 only** (`main()`
   refuses the LAN deliberately; `tailscale serve` is the front door).
5. `tailscale serve --bg 8787` — publishes it tailnet-only over HTTPS.
6. Verify: `curl -s https://mac-mini.tailXXXX.ts.net/healthz` → `ok`.
7. Run `python3 reconciler.py once`, then `python3 reconciler.py status`: watch
   `clip_synced` climb and `clip_pending` drain to zero — that is `backfill_clip()`
   PATCHing the Clip link onto every pre-existing page. New pages get the link at
   creation (`notion_properties()` adds it whenever `PUBLIC_BASE_URL` is set).
   `clip_gave_up` staying at zero confirms the property was created correctly.
8. Open a `/clip/<event_id>` link from Notion on a tailnet device and play a segment.
9. Autostart: give clipserver its own launchd plist alongside the reconciler's (see the
   `launchd/` directory and the launchd notes in LEARNING-NOTES §6–7 — `bootstrap`, not
   `load`; edit the copy in `~/Library/LaunchAgents`, not the repo file).

The presigned URLs it mints last `CLIP_URL_TTL_SECONDS` (default 5 minutes) and are
signed by the same IAM user — no access beyond Section 2 is needed.

### 5.8 Reconciler autostart

`launchd/` in the repo; procedure and gotchas in LEARNING-NOTES §6–7. Two that bite:
`bootout` is not an uninstall (`RunAtLoad` resurrects the job at next login — `rm` the
plist), and **Fregata itself needs autostart** — if the NVR does not come back after a
reboot, the reconciler snapshots a frozen database and reports a healthy-looking
"Found N events" with nothing to do, forever.

---

## 6. Decisions needed from the lead

1. **Bucket encryption** — SSE-S3 or SSE-KMS? If KMS: grant `kms:GenerateDataKey` +
   `kms:Decrypt` on the CMK (Section 3.1).
2. **Region** — for `AWS_REGION`.
3. **Lifecycle retention** — 30, 60, or 90 days under `fregata/` (~53 GB/month growth;
   90 days ≈ 160 GB steady state).
4. **`NOTION_INCLUDE_PERSON`** — explicit sign-off before real names are published to
   Notion. Default stays `false` ("Unrecognized").
5. **Prefix** — confirm `fregata/` or name the production prefix, so the policy ARN and
   `S3_PREFIX` are set together.
