# Learning Notes — camera-door-automation

A record of what we investigated, what we found, and what it all means.
Written for someone coming from Salesforce/Apex into Python, SQL, and services.

---

## 1. What this project is

`reconciler.py` watches Fregata (a native macOS build of Frigate NVR) for person
detections at the front door, finds the video segments that cover each detection,
and uploads them to S3. It is a single 389-line Python file with two dependencies:
`boto3` (AWS SDK) and `python-dotenv` (loads a `.env` file into environment variables).

It is **not** an event handler. It is a **reconciler** — a loop that wakes up on a
timer, asks "what happened?" and "what have I already delivered?", and closes the gap
between those two answers. That single design choice explains almost every unusual
thing in the file: the read-only database snapshot, the second SQLite database, the
UPSERT statements, and the fact that it can be killed mid-run and lose nothing.

On your machine, verified: Fregata is a native `.app` (not Docker), its event database
lives at `~/Fregata/config/frigate.db`, your camera is called `door_camera`, and there
are 312 finished person events representing about 21 GB of video.

---

## 2. Timeline — what we actually did

The wrong turns are the most useful part of this section. Read them.

### Step 1 — Code review of `reconciler.py`

Found 14 issues, ranging from "this will leak your credentials to GitHub" down to
"this is slightly wasteful." The two critical ones were both configuration, not logic:
`.gitignore` did not ignore `.env`, and `.env.example` described a completely different
program (the deleted JavaScript version).

### Step 2 — Built an 8-stage bring-up runbook — assuming Docker

Frigate is normally distributed as a Docker container, so the runbook was written around
container path translation, `docker inspect` for volume mounts, and so on.

### Step 3 — **Correction #1: Fregata is native, not Docker**

You said: *"This is not in Docker, Fregata is just a native application built on top of
Frigate NVR."*

This mattered more than it sounds. In Docker, the database stores container paths like
`/media/frigate/recordings/...` that don't exist on the host — so a wrong
`FREGATA_RECORDINGS_DIR` fails **loudly** with `FileNotFoundError`. On a native install,
the paths exist as stored, so a wrong value fails **silently**, producing mangled S3 keys.
The native case is more dangerous, not less.

### Step 4 — **Correction #2: "the data comes from the log file"**

You said the events come from `Fregata > logs > frigate > current` and gave us that file
to read. This turned out to be the single most instructive moment in the whole session.

### Step 5 — Read the log (29,769 lines, 3.2 MB, Aug 4 → Aug 16)

What we found:

- It is Frigate's **application log** — the program's diary of what it is doing.
- It contains real face recognition results: `Detected best face for person as: <name> with probability 0.83`
  (names redacted here — they are real people, and this repo is public)
- **But it is unusable as a data source.** The success lines have no event ID, no camera,
  no start/end time, and no video path. Across 12 days there were 687 recognitions and
  only 25 event IDs in the entire file — and those 25 appear only on *failure* lines.
  You cannot join a name to a video with this.
- It also revealed your camera was broken: 1,477 RTSP `404 Not Found` crashes on Aug 4,
  37 on Aug 5, 8 on Aug 7, zero after. You fixed it and probably didn't notice the log
  had recorded the whole thing.

### Step 6 — The log gave us the one thing we needed

Buried in the noise:

```
/Users/swarm/Fregata/media/clips/faces/<name>
/Users/swarm/Fregata/config/model_cache/facedet/landmarkdet.yaml
```

**Data root: `/Users/swarm/Fregata/`.** Which meant `~/Fregata/config/frigate.db` should
exist — and `reconciler.py`'s hardcoded defaults were right all along.

### Step 7 — Verified the database

`ls` confirmed a 24.5 MB `frigate.db`. `.tables` showed `event` and `recordings`. A sample
query returned real rows with `door_camera`, `person`, and names in `sub_label`.

### Step 8 — Clean dry run

312 events → 2,270 upload lines → 1,891 distinct files → 21 GB.
Zero `external/` keys, zero tracebacks. Everything working.

---

## 3. Verified facts

Everything in this table was confirmed on your actual machine, not assumed.

| Fact | Value | How we know |
|---|---|---|
| Fregata install | `/Applications/Fregata.app` — native macOS bundle, Python 3.11 | Paths in tracebacks in the log |
| Data root | `/Users/swarm/Fregata/` | Paths in the log |
| Event database | `~/Fregata/config/frigate.db`, 25,710,592 bytes (~24.5 MB) | `ls -la` |
| DB owner/perms | `swarm:staff`, `-rw-r--r--` — readable by you | `ls -la` |
| Tables | `event`, `recordings`, `export`, `migratehistory`, `previews`, `regions`, `reviewsegment`, `timeline`, `trigger`, `user`, `userreviewstatus` | `.tables` |
| Camera name | **`door_camera`** — not `door` | `SELECT DISTINCT camera` |
| Label | `person` | Sample rows |
| `sub_label` | Real person names — 9 distinct people recognised (redacted; public repo) | Log + sample rows |
| `sub_label` when unknown | **Empty string**, not `NULL` | Sample rows |
| Finalized person events | **312** | `SELECT COUNT(*)` |
| Event ID format | `<unix_timestamp>-<6 random chars>`, e.g. `1786809272.368455-x1xezx` | Sample rows |
| Timestamps | Unix epoch seconds as REAL (float) | Sample rows |
| Event durations seen | 2.3s to 73.2s | Sample rows |
| Recording paths | Host-absolute and exist as stored, e.g. `/Users/swarm/Fregata/media/recordings/2026-08-16/09/door_camera/03.32.mp4` | `SELECT path` + `ls` |
| Recording layout | `recordings/YYYY-MM-DD/HH/camera/MM.SS.mp4` | Path samples |
| Directory timezone | **UTC** (you are UTC+8) | File in hour `09` while local clock read ~17:00 |
| Dry run — upload lines | 2,270 | `grep -c 'DRY RUN upload /'` |
| Dry run — distinct files | 1,891 (so 379 segments are shared between overlapping events) | `sort -u \| wc -l` |
| Dry run — total size | **21 GB** | `du -ch` |
| Dry run — mangled keys | 0 | `grep -c 'external/'` |
| Dry run — errors | 0 | `grep -c Traceback` |
| Camera outage | Broken Aug 4 (1,477 RTSP 404s), fixed by Aug 5 | Log, crashes per day |
| Growth rate | 21 GB / 12 days ≈ **1.75 GB/day ≈ 53 GB/month** | Derived |

### Unverified — test these yourself before relying on them

- Whether Fregata autostarts after a reboot (it needs to, or the reconciler runs against
  a frozen database and reports success forever).
- Whether Fregata exposes Frigate's HTTP API on port 5000.
- Where Fregata stores **snapshots** (JPEGs). `media/clips/faces/<name>/` exists, but the
  per-event snapshot location was never confirmed.
- Whether the database is in WAL mode.

---

## 4. The concepts, explained simply

### Logs vs. databases — the distinction you actually need

I told you early on that "logs is the wrong word, it's a database." That was right about
`reconciler.py`'s data source, but it was too blunt, because **you do have a real log
file** and it's a legitimate thing. The accurate version:

Frigate produces **both**, and they are for different jobs.

| | The log (`~/Fregata/logs/frigate/current`) | The database (`~/Fregata/config/frigate.db`) |
|---|---|---|
| What it is | Unstructured text lines, append-only | Structured rows with typed columns |
| Job | Debugging *Fregata itself* | Recording *what happened* |
| Has names | Yes | Yes (`sub_label`) |
| Has event IDs | Only on failures (25 in 12 days) | Yes, every row |
| Has start/end times | No | Yes |
| Has video paths | No | Yes (`recordings.path`) |
| Format stability | Can change any release | Migrated deliberately |

The log was genuinely useful — it told us where the data root was and that your camera
had been broken. But it can't be your data source, because you cannot join a name to a
video with it.

**The general habit:** before deciding how to consume an upstream system, ask what shape
it already hands you. Half of integration work is discovering the hard part is done.

---

### Reconciliation loop vs. event handler

You already know both shapes in Salesforce clothing.

| | Salesforce | Nature | If it fails |
|---|---|---|---|
| Event handler | Trigger on `after insert` | Push. Fires once. | The event is gone. |
| **Reconciler** | Scheduled Batch Apex on `WHERE Status__c != 'Delivered'` | Pull. Runs on a timer, converges. | Nothing. Next run picks it up. |

Your `watch` command is the second one:

```python
while True:
    try:
        run_once(settings)
    except Exception:
        LOG.exception("Reconciliation pass failed")
    time.sleep(settings.poll_seconds)
```

It subscribes to nothing. It wakes up, looks at the world, looks at its own notes, closes
the gap, sleeps. That's it.

---

### The two-database split

This is the most important design decision in the file, and it has no Salesforce analogue
because the platform owns both halves for you.

| | `frigate.db` | `reconciler-state.db` |
|---|---|---|
| Owns the schema | Fregata | **You** — `open_state()` creates it |
| Access | Read-only, via a snapshot copy | Read/write |
| Answers | "What happened at the door?" | "What have I done about it?" |
| If deleted | Disaster | You re-upload everything; nothing is *lost* |

That last row is the test for a well-built reconciler: the state database is a *cache of
completed work*, not a source of truth. Losing it should cost money and time, never
correctness.

---

### Idempotency and at-least-once delivery

Look at the order of operations in `process_event`: the file is uploaded to S3, and
*then* a row is written to `segment_delivery` and committed. Two separate systems, with
a gap between them.

If the process dies in that gap, the file is in S3 but your state DB doesn't know. Next
pass uploads it again. That's not a bug — it's a deliberate choice with a name:

- **At-most-once** — record intent first, then act. Never duplicates, can lose work.
- **At-least-once** — act first, then record. Never loses work, can duplicate. **← this code**
- **Exactly-once** — needs a distributed transaction across S3 and SQLite. Expensive, rarely worth it.

At-least-once is only safe when repeating the action is harmless. Here it is: `PUT` to
the same S3 key twice just overwrites with identical bytes. That property is
**idempotency**, and it's what makes the whole design work.

---

### SQLite is a library, not a server

This is the answer to "how does setting up a database connection work?"

With Postgres or MySQL there's a server process listening on a port, and you authenticate
with a host, port, username and password. **SQLite has none of that.** No daemon, no port,
no credentials. The database is one ordinary file, and the driver is a library compiled
into your program that reads and writes that file directly.

```python
conn = sqlite3.connect("/Users/swarm/Fregata/config/frigate.db")
```

That line opens a file. The word "connect" is a leftover from Python's DB-API standard
(the same function name you'd call for Postgres). Read it as `open()`.

Consequences: file permissions are your access control, a backup is `cp`, "the database
is down" isn't a failure mode — but "another process has it locked" is.

---

### Connection, cursor, commit

Three objects, three jobs:

- **Connection** — the open file plus the current transaction. `close()` releases the lock.
- **Cursor** — a handle to the results of one statement. `conn.execute(...)` makes one for
  you and returns it, which is why `for row in conn.execute(sql)` works — you're iterating
  the cursor, streaming rows.
- **Transaction** — Python's sqlite3 driver silently opens one before your first write and
  holds it until you call `commit()`.

**The trap:** if you write rows and never `commit()`, they are invisible to every other
connection and are **discarded when the connection closes**. No error, no warning. That's
why you see `state.commit()` after every write. Coming from Apex — where the transaction
commits itself at the end of the execution context — this is the easiest thing to get wrong.

Also worth knowing now: `with sqlite3.connect(p) as conn` does **not** close the connection.
For sqlite3, `with` manages the *transaction* (commit on success, rollback on exception)
and leaves the connection open. Use `contextlib.closing()` if you want it closed.

---

### Parameterized values vs. escaped identifiers

Values use `?` placeholders. The driver sends the SQL and the data separately, so a value
can never be parsed as SQL. Injection isn't filtered — it's structurally impossible.

```python
conn.execute("SELECT ... WHERE camera = ? AND label = ?", (camera, label))
```

That's your `:bindVariable` in SOQL.

But table and column names are built with an f-string, which looks like the exact thing
you were just warned against:

```python
sql = f"SELECT {', '.join(qident(c) for c in select_cols)} FROM {qident(table)} ..."
```

It's correct, because **placeholders bind values only, never identifiers.** `SELECT ? FROM x`
would select a string literal, not a column. So when a name is dynamic you must build the
string and escape it yourself:

```python
def qident(name: str) -> str:
    return '"' + name.replace('"', '""') + '"'
```

Wrap in double quotes, double any embedded double quote. Direct counterpart to
`String.escapeSingleQuotes()` in dynamic SOQL.

---

### UPSERT — `Database.upsert` written longhand

```sql
INSERT INTO event_delivery(event_id, camera, start_time, end_time, updated_at)
VALUES(?,?,?,?,?)
ON CONFLICT(event_id) DO UPDATE SET
  updated_at = excluded.updated_at,
  last_error = NULL
```

"Insert; but if that violates the `event_id` primary key, update instead." `excluded` is a
pseudo-table holding the row that *would have been* inserted.

This is `Database.upsert(records, External_Id__c)`. It's what makes "record that I've seen
this event" safe to run every 30 seconds forever — no check-then-insert, which would be a
race condition anyway.

---

### The interval overlap test

This is how the code answers "which video files contain this event?":

```sql
WHERE camera = ? AND end_time >= ? AND start_time <= ?
```

Looks backwards until you recognise it. Two ranges `[a1,a2]` and `[b1,b2]` overlap **if and
only if** `a1 <= b2 AND a2 >= b1`. Read it as "A starts before B ends, and A ends after B
starts."

Memorise the shape. Calendars, bookings, rate limits, log correlation — you will write it
again.

We saw it work: your 6-second event `1786809272.368455-x1xezx`, padded by 10s before and
15s after, becomes a 31-second window, which spans **4** of Frigate's 10-second segments.
The dry run reported exactly `4 segment(s)`.

---

### `@dataclass`, and what decorators actually do

A decorator is a function that takes the thing defined below it and returns a replacement.
That's the whole idea.

You wrote:

```python
@dataclass(frozen=True)
class Settings:
    source_db: Path
    bucket: str
    dry_run: bool
```

What you got is roughly:

```python
class Settings:
    def __init__(self, source_db, bucket, dry_run):
        object.__setattr__(self, 'source_db', source_db)
        # ...
    def __repr__(self): ...      # auto-generated
    def __eq__(self, other): ... # auto-generated field-by-field
    def __setattr__(self, name, value):
        raise FrozenInstanceError(...)   # because frozen=True
```

**The Apex comparison that will mislead you:** `@AuraEnabled` and `@InvocableMethod` look
like decorators but they're **annotations** — inert markers the platform reads at compile
time. A Python decorator is live code that runs at import time and can return anything.
That's why decorators are everywhere in Python and annotations aren't in Apex.

`frozen=True` matters here because `Settings` is built once and passed to a dozen
functions. None of them can mutate it and leave the others disagreeing. Config is a
*value*, not a mutable global.

---

### Environment variables are always strings, and `"false"` is truthy

```python
def b(name: str, default: bool) -> bool:
    return os.getenv(name, str(default)).strip().lower() in {"1","true","yes","on"}
```

There is no boolean type in an environment. And in Python *every* non-empty string is
truthy — including `"false"`. So `bool(os.getenv("DRY_RUN"))` returns `True` when
`DRY_RUN=false`. This bug ships constantly.

Consequence for you: `DRY_RUN` accepts only `1`/`true`/`yes`/`on`. Every other value —
a typo, `y`, `0`, empty — means **live**. It fails toward uploading.

Same applies to numbers: `float(os.getenv("PRE_ROLL_SECONDS","10"))` converts explicitly,
and `POLL_SECONDS=30s` is a startup crash.

---

### boto3's credential chain

`s3_client()` never passes credentials. That's normal — boto3 resolves them in order:

1. Parameters passed directly to the client
2. Environment variables: `AWS_ACCESS_KEY_ID`, `AWS_SECRET_ACCESS_KEY`, `AWS_SESSION_TOKEN`
3. `~/.aws/credentials` and `~/.aws/config`
4. IAM role metadata (EC2/ECS/Lambda)

Since `load_dotenv()` pushes `.env` into `os.environ`, putting `AWS_ACCESS_KEY_ID` there
works — boto3 finds it at step 2. Putting `S3_ACCESS_KEY_ID` there, as the old
`.env.example` does, does **nothing**. Nothing reads that name.

Two gotchas:
- `load_dotenv()` runs with `override=False`, so anything already exported in `~/.zshrc`
  or sitting in `~/.aws/` **silently wins** over your `.env`.
- The client is constructed *before* `dry_run` is checked, so a broken `~/.aws` SSO
  profile can kill even the safe dry-run command.

---

### Why S3 must come before Notion

You originally described the pipeline as "upload to Notion, then S3." Reverse it.

**Notion doesn't store your video. It stores a link to your video.** A page containing a
URL to an object that doesn't exist is a broken page. The referent must exist before the
reference.

| Order | If it fails halfway |
|---|---|
| **S3 → Notion** ✅ | Video safely stored, page missing. Retry only the cheap API call. |
| Notion → S3 ❌ | A page pointing at a 404. Now you need to track "pages awaiting their video" — a second reconciliation problem you invented. |

**The general rule:** sequence steps so each depends only on durably-completed work. Where
two orderings are both valid, do the expensive, most-likely-to-fail thing first.

---

## 5. The 14 findings

| # | Severity | Issue | Where | Fix |
|---|---|---|---|---|
| 1 | 🔴 Critical | `.gitignore` doesn't ignore `.env` — it's two lines, `.agents` and `.claude`, **with no trailing newline**, so `echo .env >> .gitignore` produces `.claude.env` and ignores nothing | `.gitignore` | `printf '\n.env\nvenv/\n*.db\nlogs/\n' >> .gitignore`, then verify with `git check-ignore -v .env` |
| 2 | 🔴 Critical | `.env.example` configures a deleted program. 19 of 20 keys are dead; the one that's read (`S3_BUCKET=hackerhouse-entries`) names a bucket you don't own | `.env.example` | Rewrite from `Settings.from_env`. Never run `cp .env.example .env` |
| 3 | 🟠 High | Credentials never reach boto3 — code reads no key, boto3 wants `AWS_*` names, example file defines `S3_ACCESS_KEY_ID` | `reconciler.py:208` | Use `AWS_ACCESS_KEY_ID` / `AWS_SECRET_ACCESS_KEY`. See *boto3's credential chain* |
| 4 | 🟠 High | No watermark. Every pass rescans all 312 events + one state query each, forever. README claims "cursor holds" — there is no cursor | `:157`, `:334` | Track `MAX(start_time)` of completed events, or `ATTACH` the state DB and `LEFT JOIN` |
| 5 | 🟡 Medium | Config validated too late — the `S3_BUCKET` check lives inside `upload_file`, discovered mid-upload of event 47 | `:59-62`, `:221` | Validate everything in `from_env()`, fail before any work starts |
| 6 | 🟡 Medium | Dry run still writes to the state DB (the `event_delivery` INSERT is unconditional) | `:246-250` | Guard with `if not dry_run` — but note the insert is load-bearing, see below |
| 7 | 🟡 Medium | No backoff, no terminal state. A permanently broken event retries every 30s forever | `:251-261`, `:336-344` | Add `attempts` + `next_attempt_at`; dead-letter after N tries |
| 8 | 🟡 Medium | `finally` blocks reference possibly-unbound names and swallow every exception | `:318-321`, `:346-350` | `contextlib.closing()`, or init to `None` first |
| 9 | 🔵 Low | `false_positive` and `has_clip` are selected but never filtered — you archive false positives | `:158` | Add `false_positive = 0` to the WHERE clause |
| 10 | 🔵 Low | Schema sniffing is clever but fragile; a rename produces an unhelpful error | `:86-100` | Record the resolved schema, warn loudly when it changes |
| 11 | 🔵 Low | Whole database copied every poll — 2,880 full copies/day at 30s | `:103-116` | Skip if source mtime unchanged; raise `POLL_SECONDS` |
| 12 | 🔵 Low | ETag treated as a checksum — for multipart uploads it isn't an MD5 | `:225-226` | Use `ChecksumAlgorithm="SHA256"`; drop the extra `head_object` |
| 13 | 🔵 Low | Two supervisors: plist `KeepAlive` **and** an internal `watch` loop | `plist`, `:377-384` | Pick one. `once` + `StartInterval` is the cleaner choice |
| 14 | 🔵 Low | Ctrl-C during `time.sleep` escapes as a traceback — the handler only wraps `run_once` | `:377-384` | Move `sleep` inside the `try` |

### On #6 — why that INSERT is load-bearing

The error handler runs `UPDATE event_delivery SET last_error=? WHERE event_id=?`. An
`UPDATE` matching zero rows does nothing and **reports no error**. So if the row didn't
already exist, the failure would be silently discarded. Inserting first guarantees there's
something to attach the error to. Guarding it for dry runs is still right — but you're
accepting that dry-run failures appear only in the log, never in `status`.

---

### 🔒 Privacy — the one to fix first

Your `sub_label` column holds **real people's names** — 9 distinct people were recognised
in the log sample. The actual names are deliberately not reproduced here, because this
repository is public.

The manifest builder spreads the *entire raw event row*:

```python
"event": {
    **event,                                    # ← everything, including sub_label
    "start_time_utc": utc_iso(event.get("start_time")),
    ...
}
```

And `fetch_events` selects `sub_label`, `zones`, and `data`. So with manifests enabled,
you'd upload a name-stamped timeline of who entered your house — which directly
contradicts your own README: *"Only derived artifacts — clips and recording segments —
leave the house."*

**Right now you are protected by `UPLOAD_EVENT_MANIFEST=false` in your `.env`.** Keep it
that way until you've filtered the fields at `:284-288`. Turning it back on is a one-line
change and costs nothing to defer.

Related: `/tmp` and log files. The dry-run log records the full manifest JSON inline, so
keep those logs out of shared `/tmp` and delete them when done.

---

### ❓ Your question: does the code delete anything?

**Almost nothing.** The only deletion in the entire file is:

```python
snap.unlink(missing_ok=True)
```

at lines **321** and **350** — and that deletes only its *own temporary snapshot* of the
Frigate database, the copy it made at the start of the pass. It's inside a `finally` block
so it runs even when the pass crashes.

Specifically, `reconciler.py` **never**:

- deletes recordings from your disk
- deletes anything from S3
- deletes rows from `frigate.db` (it opens that file read-only, `mode=ro` — writing is
  structurally impossible)
- deletes rows from its own state database

Two consequences:

1. **Nothing cleans up S3.** No retention, no lifecycle. At ~1.75 GB/day that's ~53 GB/month
   growing forever. Set a bucket lifecycle rule to expire objects after 30 or 90 days.
2. **Nothing frees local disk.** Fregata's own retention settings handle that, not this code.

---

## 6. Where we are now, and what's next

### Current state ✅

- Repo cloned to the Mac Mini, on `main`, venv active
- `.gitignore` fixed, `.env` written by hand with your real values
- Dry run **clean**: 312 events, 1,891 distinct files, 21 GB, zero mangled keys, zero errors
- Decision made: **AWS S3** (you have credits)

Your working `.env`:

```
FREGATA_DB_PATH=/Users/swarm/Fregata/config/frigate.db
FREGATA_RECORDINGS_DIR=/Users/swarm/Fregata/media/recordings
STATE_DB_PATH=/Users/swarm/<your-repo-path>/reconciler-state.db
CAMERA=door_camera
LABEL=person
DRY_RUN=true
UPLOAD_EVENT_MANIFEST=false
```

### Next — create the bucket

Create it, then set three things **before** the first upload:

- **Block Public Access: ON.** Nothing in the code can make an object public (no ACLs, no
  presigning), so any exposure would come from bucket settings alone.
- **Versioning: OFF.** Segments shared between overlapping events get PUT twice under the
  same key — harmless without versioning, silent growth with it.
- **Lifecycle rule** expiring objects after 30–90 days. At 53 GB/month, this is the
  difference between a stable bill and 640 GB/year.

### Next — IAM policy (minimum)

```json
{
  "Version": "2012-10-17",
  "Statement": [{
    "Effect": "Allow",
    "Action": [
      "s3:PutObject",
      "s3:GetObject",
      "s3:AbortMultipartUpload"
    ],
    "Resource": "arn:aws:s3:::YOUR-BUCKET/fregata/*"
  }]
}
```

**`s3:GetObject` is not optional.** `upload_file` is followed immediately by `head_object`
with no `try/except`, and HeadObject maps to `s3:GetObject`. Grant write-only and you get
the worst failure mode: every upload lands in the bucket **and then throws** — so the
segment row is never written and the same file re-uploads every 30 seconds, billing you
each time.

No bucket-level ARN needed; nothing lists.

### Next — add to `.env`

```
S3_BUCKET=your-bucket-name
S3_PREFIX=fregata
AWS_REGION=us-east-1
AWS_ACCESS_KEY_ID=...
AWS_SECRET_ACCESS_KEY=...
```

Do **not** set `S3_ENDPOINT_URL` for AWS — leave it out entirely.

### Next — preflight before pushing 21 GB

See the commands section. It exercises all three API calls the program makes and gives each
failure its own distinct error, for the cost of two objects.

### Next — go live

1. `DRY_RUN=false`
2. `reconciler.py once` — this backfills all 312 events / 21 GB. At 10 Mbps up that's ~5
   hours; at 50 Mbps ~1 hour. Start it when you don't need the bandwidth.
3. `reconciler.py status`
4. `reconciler.py once` **again** — the convergence test. Assert **both** that it finds the
   same N **and** completes ~zero. Zero completions alone can mean "converged" or "found
   nothing," which are very different.
5. Integrity check: compare `head_object` `ContentLength` against local `stat -f%z`, then
   download one clip and actually play it. An object listing shows a name — it passes just
   as happily for a truncated file.

### ⚠️ Rollback — read before you need it

If the first live pass lands objects under `external/` keys, **fixing the config and
re-running does nothing.** Those events are marked complete and short-circuit forever.
Clearing only `completed_at` doesn't help either: the segment rows survive, so the code
reuses the old ETags, skips the uploads, and writes a fresh manifest pointing at the old
broken keys.

The only clean reset:

```bash
rm reconciler-state.db reconciler-state.db-wal reconciler-state.db-shm
```

That means paying for the full backfill twice, and orphaned objects must be deleted from
the console (your minimum IAM policy deliberately excludes `s3:DeleteObject`).

Your dry run showed **zero** `external/` keys, so you're clear — but this is why that check
mattered.

### Later — launchd autostart

Nothing in the plist is on the path to a first upload, so do it last. Notes:

- `mkdir -p logs` first — git can't store an empty dir and launchd won't create it
- Edit the **copy** in `~/Library/LaunchAgents`, not the repo file
- All five `YOURUSER` occurrences share `/Users/YOURUSER/entry-logger`, so one `sed` covers them
- `launchctl bootstrap gui/$(id -u) ...` gives real errors; `launchctl load` reports almost
  everything as `Input/output error`
- `out.log` staying empty is **normal** — all logging goes to stderr
- **`bootout` is not an uninstall.** `RunAtLoad` brings it back at next login; `rm` the plist
- FileVault blocks auto-login, so the README's power-cut plan doesn't work on an encrypted Mac
- **Fregata needs its own autostart.** If it doesn't come back after a reboot, the reconciler
  snapshots a frozen database, logs `Found N events` every 30s, uploads nothing, and exits 0
  forever — a dead system that looks perfectly healthy

### Deferred backlog

| Priority | Item |
|---|---|
| First | Filter manifest fields, then re-enable `UPLOAD_EVENT_MANIFEST` |
| First | Repo hygiene — rewrite `.env.example`, fix README (`cp .env.example .env`, "Python 3.12+", the "cursor holds" claim) |
| High | The watermark (finding #4) |
| High | Retry state and backoff (finding #7) |
| Medium | Supervision redesign (finding #13) |
| Medium | Log rotation — nothing rotates `err.log` |
| Medium | Snapshots — the feature you thought you had. Verify your layout first |
| Low | Findings #6, #9, #11, #12 |
| Last | Notion and Slack sinks — with **per-sink delivery rows from the start** |

On that last one: `completed_at` can't stay a single flag once you have multiple
destinations, because "uploaded to S3" and "posted to Notion" fail independently. If Notion
is down, the next pass must retry Notion only — not re-upload 21 GB.

```sql
CREATE TABLE IF NOT EXISTS sink_delivery (
  event_id     TEXT NOT NULL,
  sink         TEXT NOT NULL,        -- 's3' | 'notion' | 'slack'
  delivered_at REAL,
  attempts     INTEGER NOT NULL DEFAULT 0,
  last_error   TEXT,
  PRIMARY KEY(event_id, sink)
);
```

Design this in from the start. Retrofitting after hard-coding a single completion flag is
genuinely painful.

---

## 7. Command reference

### Explore the database

```bash
# Does it exist?
ls -la ~/Fregata/config/frigate.db

# What tables?
sqlite3 -readonly ~/Fregata/config/frigate.db ".tables"

# Full schema for one table
sqlite3 -readonly ~/Fregata/config/frigate.db ".schema event"

# Your exact camera and label values
sqlite3 -readonly ~/Fregata/config/frigate.db "SELECT DISTINCT camera FROM event;"
sqlite3 -readonly ~/Fregata/config/frigate.db "SELECT DISTINCT label FROM event;"

# Recent events
sqlite3 -readonly ~/Fregata/config/frigate.db \
  "SELECT id, camera, label, sub_label, start_time, end_time
   FROM event ORDER BY start_time DESC LIMIT 5;"

# Backfill size (include the camera predicate — the reconciler filters on it too)
sqlite3 -readonly ~/Fregata/config/frigate.db \
  "SELECT COUNT(*) FROM event
   WHERE end_time IS NOT NULL AND label='person' AND camera='door_camera';"

# Where the video actually lives
sqlite3 -readonly ~/Fregata/config/frigate.db \
  "SELECT path FROM recordings ORDER BY start_time DESC LIMIT 3;"
```

### Explore the log

```bash
L=~/Fregata/logs/frigate/current

# Time range
head -1 $L; tail -1 $L

# Which subsystems are noisy
sed -E 's/^\[[^]]*\] +//; s/ +(DEBUG|INFO|WARNING|ERROR|CRITICAL) +:.*$//' $L \
  | sort | uniq -c | sort -rn | head -20

# Face recognitions per day (a proxy for "was the camera working")
grep 'Detected best face' $L | grep -oE '^\[[0-9-]+' | tr -d '[' | sort | uniq -c

# Camera failures per day
grep 'Error opening input file rtsp' $L | grep -oE '^\[[0-9-]+' | tr -d '[' | sort | uniq -c
```

### The program itself

```bash
python3 reconciler.py inspect   # schema discovery — no creds, no state DB, safe
python3 reconciler.py once      # one pass
python3 reconciler.py status    # delivery counts + recent failures
python3 reconciler.py watch     # loop forever
```

### Dry run and measure

```bash
python3 reconciler.py once 2>&1 | tee dryrun.log
echo "exit=${pipestatus[1]}"    # zsh. tee hides the real exit code — check immediately

grep -c Traceback dryrun.log            # must be 0
grep -c 'external/' dryrun.log          # must be 0 — mangled keys
grep -c 'DRY RUN upload /' dryrun.log   # upload lines

# Distinct files (dedups segments shared between events)
grep 'DRY RUN upload /' dryrun.log \
  | sed 's/^.*DRY RUN upload //; s/ -> s3:.*$//' | sort -u | wc -l

# Total bytes
grep 'DRY RUN upload /' dryrun.log \
  | sed 's/^.*DRY RUN upload //; s/ -> s3:.*$//' \
  | sort -u | tr '\n' '\0' | xargs -0 du -ch | tail -1
```

### Credential preflight

```bash
dd if=/dev/zero of=/tmp/preflight-10m.bin bs=1m count=10   # >8MiB forces multipart

python3 - <<'PY'
import os, boto3
from botocore.config import Config
from dotenv import load_dotenv
load_dotenv()
c = boto3.client('s3',
    region_name=os.getenv('AWS_REGION', 'us-east-1'),
    endpoint_url=os.getenv('S3_ENDPOINT_URL') or None,
    config=Config(retries={'max_attempts': 8, 'mode': 'standard'}))
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
PY
```

All four lines must print. Delete the two `_preflight` objects from the console afterwards
— the minimum policy excludes `s3:DeleteObject`.

**If `PutObject` succeeds but `HeadObject` 403s: stop.** That's the missing-`s3:GetObject`
failure, and it will re-upload forever.

### Secrets hygiene

```bash
printf '\n.env\nvenv/\nreconciler-state.db*\nlogs/\n__pycache__/\n' >> .gitignore
git check-ignore -v .env          # must print a rule and exit 0
tail -3 .gitignore                # confirm no mangled ".claude.env" line
chmod 600 .env
git status --porcelain            # .env must NOT appear
```

### Inspect the state database

```bash
sqlite3 reconciler-state.db ".tables"
sqlite3 reconciler-state.db "SELECT COUNT(*) FROM event_delivery WHERE completed_at IS NOT NULL;"
sqlite3 reconciler-state.db "SELECT event_id, last_error FROM event_delivery WHERE last_error IS NOT NULL LIMIT 10;"
```

### launchd

```bash
mkdir -p ~/entry-logger/logs
cp launchd/com.swarm.entry-logger.plist ~/Library/LaunchAgents/
sed -i '' "s/YOURUSER/$(id -un)/g" ~/Library/LaunchAgents/com.swarm.entry-logger.plist
grep -c YOURUSER ~/Library/LaunchAgents/com.swarm.entry-logger.plist   # must be 0

# The real check — does the resolved interpreter exist?
ls -l "$(plutil -extract ProgramArguments.0 raw ~/Library/LaunchAgents/com.swarm.entry-logger.plist)"

plutil -lint ~/Library/LaunchAgents/com.swarm.entry-logger.plist
launchctl bootstrap gui/$(id -u) ~/Library/LaunchAgents/com.swarm.entry-logger.plist
launchctl print gui/$(id -u)/com.swarm.entry-logger
tail -f ~/entry-logger/logs/err.log

# Stop (until next login) / reload after edit / actually uninstall
launchctl bootout      gui/$(id -u)/com.swarm.entry-logger
launchctl kickstart -k gui/$(id -u)/com.swarm.entry-logger
rm ~/Library/LaunchAgents/com.swarm.entry-logger.plist
```

---

## The five habits worth keeping

Everything above is specific to this project. These generalise.

1. **Run it and watch it fail.** You cannot review code you haven't executed. The dry run
   told us more in 30 seconds than an hour of reading.
2. **Trace one record end to end.** We followed event `1786809272.368455-x1xezx` from a
   database row to `4 segment(s)` and the maths checked out. Anything you can't account
   for, you don't understand yet.
3. **Grep every config key the code reads and diff it against your `.env`.** That one
   exercise would have caught the three worst bugs in this repo.
4. **Ask "what happens if this dies right here?" at every external call.** That question
   is what produced the state database.
5. **Treat the README as a claim to verify, not a description.** Yours described a cursor
   that was never written and a Python version that isn't required. AI-written
   documentation drifts fastest, because it describes the intended design rather than the
   shipped one.

**And the one that matters most here:** AI-written code is uniformly confident whether or
not it's finished. In this repo, genuinely expert work — the read-only snapshot, the
container path translation, the settle check, the pre-roll padding — sits directly next to
a `.gitignore` that would have leaked your credentials. Nothing in the code's *texture*
distinguishes the two. Human juniors write code that looks junior. This doesn't.
