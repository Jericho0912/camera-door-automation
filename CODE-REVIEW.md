# Code review — `reconciler.py`

Reviewed at 484 lines / 22 functions / 1 class, including the uncommitted Notion
work. Every claim below is backed by a test in `tests/`; the 12 defects are
encoded as `strict=True` xfails, so fixing one turns the suite red until the
marker is removed.

---

## 1. The god-class question

**There is no god class.** The file defines exactly one class:

```
Settings: 18 fields, 1 method (from_env)
```

A frozen dataclass with one factory method is the opposite of a god class — it
has almost no behaviour at all. Everything else is a module-level function.

But the instinct is picking up something real. The god-object smell is present;
it just lives in two places that aren't classes.

### 1a. `process_event` is a god *function*

| function | lines | params | branches |
|---|---:|---:|---:|
| **`process_event`** | **69** | **7** | **12** |
| `open_state` | 33 | 1 | 0 |
| `sync_notion` | 33 | 5 | 8 |
| `run_once` | 26 | 1 | 5 |
| *(next 18 functions)* | ≤18 | ≤6 | ≤6 |

It is nearly twice the size of anything else and does six distinct jobs in one
scope: completion check → segment query → state insert → per-file validation →
upload loop with interleaved state writes → manifest construction → manifest
upload → completion write → Notion hand-off. Six reasons to change, one
function. That is the god-class problem with the word "class" removed.

The practical cost shows up as **temporal coupling**: the ordering of the state
writes relative to the uploads is load-bearing for crash-safety, but nothing in
the code expresses or enforces that ordering. Defect #4 below is exactly this
ordering being wrong in one spot.

### 1b. `Settings` is a god *parameter object*

```
18 fields · read 44 times · passed to 10 of 22 functions
```

Every function that takes `settings` can reach every configuration value in the
program. Concretely:

- `notion_request` is handed all 18 fields to read **2** (`notion_token`, `notion_version`)
- `upload_file` is handed all 18 to read **2** (`bucket`, `dry_run`)

This is textbook feature envy, and it is why the S3 layer, the SQLite layer, the
Notion layer and the CLI are all transitively coupled to one another despite
having nothing to do with each other. Nothing stops a future edit to the Notion
code from reading `settings.recordings_dir`, and nothing would flag it.

The fix is not "introduce classes". It is to narrow the parameters:

```python
def upload_file(client, bucket: str, dry_run: bool, source: Path, key: str) -> str | None:
def notion_request(method, path, auth: NotionAuth, payload=None) -> dict:
```

Each function then declares what it actually depends on, and the dependency
graph becomes visible in the signatures. `Settings` stays as the single parsing
point at the edge — that part is good design and worth keeping.

### 1c. The module is doing six jobs

One 484-line file holds: configuration, Frigate schema discovery, Frigate
queries, local state persistence, S3 delivery, Notion sync, and the CLI. The
Notion feature is the evidence — a single feature required edits in five places
(`Settings`, `open_state`, `process_event`, `status`, `requirements.txt`).
That blast radius in a *single-file program* is the signal.

At this size it is still manageable. The seam to cut first, when it stops being
manageable, is `notion` — it is the only concern with no data dependency on any
other, and it is the one most likely to be swapped out.

### 1d. Missing domain types

Events and segments are `dict[str, Any]` from the SQLite row all the way into
the manifest and the Notion payload. Nothing validates shape at the boundary,
which is the direct cause of defect #6 — a BLOB in one column crashes JSON
serialisation ~60 lines away from where it entered, *after* the uploads have
been paid for.

---

## 2. Defects

Ordered by operational severity. Each maps to a test.

### #1 — Failures before the first state write vanish silently
`reconciler.py:437` · `test_cli.py::test_a_failure_before_the_first_insert_is_still_recorded`

`run_once`'s error handler is an `UPDATE ... WHERE event_id=?`. `process_event`
does not `INSERT` the row until line 340, after `float(event["start_time"])` and
the segment query. Anything that throws before that point updates **zero rows** —
the error is logged once and then dropped from the state DB entirely. `status`
reports a clean system while the event is never delivered.

Fix: `INSERT ... ON CONFLICT DO UPDATE` in the handler, not `UPDATE`.

### #2 — Losing the state DB duplicates every Notion page
`reconciler.py:293` · `test_notion.py::test_losing_the_state_db_does_not_duplicate_pages`

Notion `POST /pages` has no upsert, so dedupe rests entirely on the local state
DB. The search-before-create guard only runs `if row:` — i.e. only when a
previous attempt already left a row. With no row, it creates blind.

This is not hypothetical. `.env.example` already warns that `STATE_DB_PATH`
defaults to a *relative* path, so running the service from a different working
directory creates a second empty state DB. Today that means re-uploading
everything to S3 (wasteful but idempotent — same keys). With Notion it means a
duplicate page for every event ever seen.

Fix: query Notion before creating, unconditionally; or make the state DB path
absolute-or-fail at startup.

### #3 — Ctrl-C during the poll interval crashes instead of exiting cleanly
`reconciler.py:480` · `test_cli.py::test_ctrl_c_during_the_sleep_exits_cleanly`

```python
while True:
    try:
        run_once(settings)
    except KeyboardInterrupt:
        return 130
    except Exception:
        LOG.exception("Reconciliation pass failed")
    time.sleep(settings.poll_seconds)   # <-- outside the try
```

The process spends nearly all of its wall-clock time in that `sleep`, which is
precisely where a Ctrl-C will land. The handler that was written to give a clean
`130` almost never gets the chance; the operator sees a traceback instead.

### #4 — A completed event is marked done before Notion sync, but an S3 failure is not isolated from it
`reconciler.py:393-398` · `test_process_event.py::test_notion_failure_does_not_undo_a_completed_delivery`

The ordering here is actually *correct* and worth stating explicitly, because it
is easy to break: `completed_at` is written before `sync_notion` runs, so a
Notion outage cannot make a successfully-archived event look undelivered. The
test pins this. The problem is that nothing in the code says so — it is an
invariant living only in statement order inside a 69-line function (§1a).

### #5 — `snapshot_db` leaks a temp file on every failed poll
`reconciler.py:110` · `test_schema_discovery.py::test_failed_snapshot_leaves_no_temp_file`

`mkstemp` runs before the backup; only the *caller's* `finally` unlinks it. If
`src.backup(dst)` raises — corrupt DB, locked DB, disk full — the temp file
stays. Under `watch` that is one leaked file every `POLL_SECONDS`, forever.

Fix: wrap the backup in `try/except` inside `snapshot_db` and unlink on failure.

### #6 — A BLOB in the event row crashes manifest generation after the uploads
`reconciler.py:374` · `test_process_event.py::test_a_blob_data_column_does_not_break_the_manifest`

`parse_json` returns non-`str` values untouched, so a BLOB `data` column arrives
as `bytes`, gets spread into the manifest via `**event`, and `json.dumps` raises
`TypeError`. The segments are already uploaded and billed at that point, and the
event can never reach `completed_at`, so it retries — and re-fails — forever.

Fix: coerce or drop non-JSON-serialisable values when building the manifest
(`json.dumps(..., default=str)` is the one-line version).

### #7 — A permanently broken event retries forever
`reconciler.py:431` · `test_process_event.py::test_a_permanently_failing_event_is_eventually_given_up_on`

No attempt counter, no backoff, no dead-letter state. One event whose segment
file was deleted will log a full traceback every `POLL_SECONDS` indefinitely,
burying real failures in the log.

Fix: an `attempts` column and an `abandoned_at` state, or exponential backoff
keyed on `updated_at`.

### #8 — `watch` discards the failure signal
`reconciler.py:475` · `test_cli.py::test_watch_surfaces_persistent_failures`

`run_once` returns `1` when events failed. The `watch` loop ignores the return
value. A launchd-managed service therefore has no way to know anything is wrong;
only `status` reveals it, and nothing runs `status`. At minimum, log a warning
when a pass reports failures.

### #9 — `Retry-After` as an HTTP-date crashes the request
`reconciler.py:264` · `test_notion.py::test_http_date_retry_after_is_tolerated`

```python
time.sleep(float(resp.headers.get("Retry-After") or 2 ** attempt))
```

RFC 9110 permits `Retry-After` to be an HTTP-date. `float("Wed, 21 Oct 2015...")`
raises `ValueError`, converting a routine rate limit into a hard event failure.

### #10 — The dry-run manifest log leaks the person's name
`reconciler.py:246` · `test_upload.py::test_dry_run_manifest_does_not_log_personal_names`

`.env.example` is careful about this — it warns that `sub_label` is a real
person's name and defaults `UPLOAD_EVENT_MANIFEST=false`. But the dry-run branch
of `upload_manifest` writes the entire manifest, `sub_label` included, to the log
at `INFO`. The privacy control is bypassed by the rehearsal mode that exists to
make the system safe to try out.

### #11 — No startup validation of the config
`test_settings.py::test_negative_padding_is_rejected`, `::test_live_mode_without_bucket_is_rejected_at_startup`

- `DRY_RUN=false` with an empty `S3_BUCKET` is accepted, then fails per-event
  inside `upload_file` — a `watch` loop that fails 100% of events and keeps going.
- A negative `PRE_ROLL_SECONDS` silently inverts the archive window rather than
  being rejected.

`Settings.from_env` is the right place for a `__post_init__` check.

### #12 — Path traversal: uploads are not constrained to the media root
`reconciler.py:198` · `test_helpers.py::test_traversal_outside_the_media_root_is_refused`

`canonical_path` returns any absolute path that exists, and
`relative_recording_key` quietly files anything outside `recordings_dir` under
`external/<hash>-<name>` rather than refusing it. A `recordings.path` value
pointing anywhere on the filesystem is read and uploaded to S3.

The trust model makes this low-severity — the source DB is local Frigate — but
it is a full local-file-exfiltration primitive gated only on "nothing ever
writes a bad path into that column", and there is no reason to accept it. Reject
resolved paths that fall outside `recordings_dir`.

---

## 3. Smaller items

- **Dead code.** `reconciler.py:269` (`raise RuntimeError(... exhausted retries)`)
  is unreachable: `range(4)` makes the last attempt `3`, the retry guard is
  `attempt < 3`, so a 4th `429` falls into the `>= 400` branch and raises there.
  Coverage confirms the line is never hit.
- **Name collision.** The module-level function `upload_manifest` and the
  `Settings.upload_manifest` field share a name. Legal, but
  `if settings.upload_manifest: upload_manifest(...)` reads badly.
- **Shadowed builtin.** `inspect` shadows the stdlib module name. Harmless today
  only because the module is never imported.
- **Unbounded rescan.** `fetch_events` has no lower time bound, so every poll
  re-reads and re-checks the entire event history. Fine at 50 events; not at
  500k. Bound it on `start_time > (last completed)`.
- **`__pycache__/` is committed-adjacent** — untracked but present, and
  `.gitignore` does not cover it (it lists only `.agents` and `.claude`, and has
  no trailing newline).

---

## 4. What's good

Worth saying, because these are the decisions that made the system testable at
all and they should survive any refactor:

- **`Settings` is frozen and parsed in exactly one place.** This is the single
  seam that made 214 tests possible without patching `os.environ` everywhere.
- **Schema discovery instead of hard-coded table names.** `resolve_table` falls
  back to structural matching and *refuses to guess* when ambiguous. Archiving
  the wrong footage is worse than failing loudly.
- **The read-only snapshot.** Each pass gets one consistent view and never
  touches the live NVR database.
- **Segment-level delivery state.** Resumption after a mid-event crash re-uploads
  nothing already delivered — verified end-to-end.
- **The 429-only retry policy, with the reasoning in a comment.** Deliberately
  *not* retrying 5xx because a blind retry could duplicate a created page is
  exactly the right call, and the comment saying so is worth more than the code.
- **`.env.example` documents failure modes, not just variable names.** It is the
  best-written file in the repo.
