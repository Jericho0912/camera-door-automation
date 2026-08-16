# Test suite

214 tests over `reconciler.py`, at 99% line coverage.

## Running

```bash
python3 -m venv .venv && source .venv/bin/activate   # or: uv venv .venv --python 3.12
pip install -r requirements-dev.txt
pytest
```

Expect **202 passed, 12 xfailed** in roughly 30 seconds.

Coverage:

```bash
coverage run --source=reconciler -m pytest && coverage report -m
```

## What the 12 xfails mean

They are **not** flaky or optional. Each one is a `@pytest.mark.defect` +
`@pytest.mark.xfail(strict=True)` pair that asserts the *correct* behaviour and
is currently expected to fail. `strict=True` means that if someone fixes the
underlying defect, the test flips to `XPASS` and the suite goes **red** — so the
list can never silently drift out of date. Fix the defect, delete the two
marker lines, and the test becomes a normal regression guard.

`CODE-REVIEW.md` explains each one. To see them:

```bash
pytest -m defect -rx
```

## Layout

| File | Covers |
|---|---|
| `conftest.py` | Frigate-shaped SQLite builder, on-disk segments, moto S3, wired `pipeline` |
| `test_settings.py` | Env parsing, bool coercion, path expansion, validation gaps |
| `test_helpers.py` | `utc_iso`, `qident`, `parse_json`, path canonicalisation and keying |
| `test_schema_discovery.py` | `resolve_table` across schema variants, snapshot isolation |
| `test_state_and_queries.py` | State DDL/idempotency, `fetch_events`, `fetch_segments` |
| `test_upload.py` | S3 client config, upload, manifest, dry run |
| `test_notion.py` | HTTP contract, retries, property mapping, sync idempotency |
| `test_process_event.py` | End-to-end delivery, resumption, failure modes |
| `test_cli.py` | `inspect` / `once` / `status` / `watch`, exit codes, temp-file hygiene |

## Notes

- **No network and no real AWS.** `moto` fakes S3 through the real botocore
  stack; `responses` fakes Notion. A session-scoped autouse fixture pins fake
  AWS credentials so a stray call can never reach a real account.
- **The S3 client is session-scoped on purpose.** Constructing a boto3 client
  loads botocore's service model from disk, which costs ~5s per call when the
  checkout sits on a Windows drive mounted into WSL (`/mnt/c/...`). Building it
  once took the suite from 140s to 22s. Each test still gets a freshly emptied
  bucket.
- **Time is controlled, not slept through.** Segment ages are set with
  `os.utime`; Notion backoff patches `time.sleep` and asserts the delays.
