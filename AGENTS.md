# fregata-reconciler — agent guide

A single-file Python service. `reconciler.py` polls a Frigate-compatible NVR
("Fregata") SQLite database on a Mac Mini, uploads door-camera event footage to
S3/R2, and optionally mirrors each event to a Notion database — including a
presigned clip link. Everything else in the repo supports that one file.

## Map

- `reconciler.py` — the whole service: `Settings`, S3, Notion, clip links, CLI.
  The frozen `Settings` dataclass is the single injection seam; every function
  takes it, and the tests rely on that.
- `.env.example` — the authoritative register of every variable the service
  reads ("anything not listed here is ignored") and the reasoning behind each
  default. Any change to configuration lands here in the same commit.
- `tests/` — hermetic pytest suite; `tests/README.md` covers fixtures and
  conventions (moto for S3, `responses` for Notion, no network ever).
- `CODE-REVIEW.md` — numbered ledger of known defects; each maps to an xfail.
- `test-notion.py` — preflight that diffs the live Notion database schema
  against what the reconciler will write. Run it before any live Notion run.
- `launchd/` — macOS autostart plist; deployment is covered in `README.md`.
- `LEARNING-NOTES.md`, `review-page.html`, `notion-database-template.csv` —
  artifacts, not code.

## Run and test

Python 3.12+.

```bash
python3 -m venv .venv && source .venv/bin/activate   # or: uv venv --python 3.12
pip install -r requirements-dev.txt                  # includes runtime deps
pytest
```

Green looks like "N passed, M xfailed" — the xfails are documented defects
(next section), never flakes. Coverage:
`coverage run --source=reconciler -m pytest && coverage report -m`.

CLI: `python3 reconciler.py {inspect,once,watch,status,clips-reset}` — see
`README.md` for what each does. There is no live NVR or bucket in a dev
environment, so exercise behaviour through the tests, not by running `once`.

## The defect ledger

A confirmed defect is encoded as a test marked `@pytest.mark.defect` +
`@pytest.mark.xfail(strict=True)` whose assertion states the **correct**
behaviour, plus a numbered entry in `CODE-REVIEW.md`. `strict=True` means
fixing the defect flips the test to XPASS and the suite goes red — so a fix
includes deleting the two marker lines and updating `CODE-REVIEW.md`. Fix the
code to meet the assertion; the assertion itself already describes the target.
`pytest -m defect -rx` lists the open ones.

## Invariants

**Privacy.** The service observes people at a front door, and every output
path defaults to publishing nothing about who they are:

- `sub_label` is a real person's name from face recognition. It reaches Notion
  only under `NOTION_INCLUDE_PERSON=true` and the S3 manifest only under
  `UPLOAD_EVENT_MANIFEST=true` — both default off in code as well as in
  `.env.example`, so a deleted line stays off. Any new output path (logs,
  messages, files committed to this public repo) starts name-free the same way.
- The Slack end-of-day summary (branch `slack-daily-summary`, PR #8, unmerged)
  has a fixed contract: one digest per day of *unrecognized* visitors only —
  no per-event pings, no personal names, no presigned URLs; each line links to
  the event's access-controlled Notion page instead. Changes touching Slack
  preserve that contract.

**Clip links are presigned, not served.** No server, no VPN, no tailscale —
the link in Notion is a presigned S3 URL to a viewer page in the bucket, and
the same poll loop that uploads footage re-signs any link older than
`CLIP_REFRESH_SECONDS`. A presigned URL is a bearer token and re-signing never
revokes: the kill switch is deactivating the dedicated read-only signing key
(`CLIP_AWS_*`). The "Know what you are enabling" list in `README.md` is the
threat model — keep it true when changing this area. Recovery from a broken
setup is `clips-reset`, which re-signs everything on the next pass.

**Delivery ordering.** In `run_once`, `refresh_clip_links` runs *after* the
delivery loop so a slow Notion day can never delay footage leaving the house.
Inside `process_event`, the interleaving of state writes with uploads is
load-bearing for crash-safety (`CODE-REVIEW.md` §1a) — preserve write order
when editing either.

**Fail toward safety, which direction depends on the flag.** Boolean env
parsing accepts only `1/true/yes/on`; anything else — typo, empty string — is
false. For `DRY_RUN` that means false = LIVE uploads; for the privacy flags it
means false = publish nothing. Keep new booleans on the same `b()` helper and
choose the default so a mangled value fails toward the safe side.

## Gotchas

- Read `.env.example` before touching configuration — it documents traps the
  code can't (empty value ≠ absent value; `AWS_REGION` mismatch breaks
  presigned links at click time only; `STATE_DB_PATH` relative to cwd silently
  re-uploads everything).
- The source schema is discovered, not assumed: `resolve_table` accepts
  `event`/`events` and `recordings`/`recording`. Go through it rather than
  hard-coding a table name.
- SigV4 caps presigned TTLs at 7 days; `Settings.from_env` clamps
  `CLIP_URL_TTL_SECONDS` and rejects `CLIP_REFRESH_SECONDS >= TTL` at startup.
- On a Windows-mounted checkout under WSL (`/mnt/c/...`), constructing a boto3
  client costs ~5s, which is why the test S3 client is session-scoped. Keep
  new S3 tests on that shared fixture (each test still gets an emptied bucket).
