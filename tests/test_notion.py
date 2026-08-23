"""Notion sync: HTTP contract, property mapping, and delivery idempotency.

Notion page creation is the one non-idempotent side effect in the program —
POST /pages has no upsert — so the dedupe logic here carries real weight.
"""
from __future__ import annotations

import json
import sqlite3

import pytest
import responses

import reconciler as rec
from conftest import NOW, make_event

PAGES = f"{rec.NOTION_API}/pages"


@pytest.fixture
def notion_settings(settings_factory):
    return settings_factory(dry_run=False, notion_token="secret_tok",
                            notion_database_id="db123", notion_version="2026-03-11")


@pytest.fixture
def http():
    with responses.RequestsMock(assert_all_requests_are_fired=False) as m:
        # notion_targets() resolves the parent shape once. An empty data_sources list means
        # the workspace predates the 2025-09-03 split, so it falls back to a database_id parent.
        m.add(responses.GET, f"{rec.NOTION_API}/databases/db123",
              json={"data_sources": []}, status=200)
        yield m


@pytest.fixture(autouse=True)
def reset_notion_targets():
    """notion_targets caches in a module global; it must not leak between tests."""
    rec._NOTION_TARGETS = None
    yield
    rec._NOTION_TARGETS = None


@pytest.fixture(autouse=True)
def no_sleep(monkeypatch):
    """Retry backoff must not add real seconds to the suite."""
    slept = []
    monkeypatch.setattr(rec.time, "sleep", lambda s: slept.append(s))
    return slept


# --------------------------------------------------------------------------
# notion_request
# --------------------------------------------------------------------------

def test_request_sends_auth_and_version_headers(http, notion_settings):
    http.add(responses.POST, PAGES, json={"id": "p1"}, status=200)
    rec.notion_request("POST", "/pages", notion_settings, {"a": 1})

    sent = http.calls[0].request
    assert sent.headers["Authorization"] == "Bearer secret_tok"
    assert sent.headers["Notion-Version"] == "2026-03-11"
    assert sent.headers["Content-Type"] == "application/json"
    assert json.loads(sent.body) == {"a": 1}


def test_request_returns_the_decoded_body(http, notion_settings):
    http.add(responses.POST, PAGES, json={"id": "p1", "object": "page"}, status=200)
    assert rec.notion_request("POST", "/pages", notion_settings)["id"] == "p1"


def test_rate_limit_is_retried_and_eventually_succeeds(http, notion_settings, no_sleep):
    http.add(responses.POST, PAGES, json={}, status=429, headers={"Retry-After": "1"})
    http.add(responses.POST, PAGES, json={}, status=429, headers={"Retry-After": "1"})
    http.add(responses.POST, PAGES, json={"id": "p1"}, status=200)

    assert rec.notion_request("POST", "/pages", notion_settings)["id"] == "p1"
    assert len(http.calls) == 3
    assert no_sleep == [1.0, 1.0], "must honour the Retry-After header"


def test_rate_limit_without_a_header_backs_off_exponentially(http, notion_settings, no_sleep):
    for _ in range(3):
        http.add(responses.POST, PAGES, json={}, status=429)
    http.add(responses.POST, PAGES, json={"id": "p1"}, status=200)

    rec.notion_request("POST", "/pages", notion_settings)
    assert no_sleep == [1, 2, 4]


def test_persistent_rate_limiting_gives_up(http, notion_settings):
    for _ in range(4):
        http.add(responses.POST, PAGES, json={}, status=429)
    with pytest.raises(RuntimeError, match="failed 429"):
        rec.notion_request("POST", "/pages", notion_settings)


@pytest.mark.parametrize("status", [400, 401, 403, 404, 409, 500, 502, 503])
def test_other_errors_are_not_retried(http, notion_settings, status):
    """Deliberate: retrying a 5xx blind could duplicate a created page."""
    http.add(responses.POST, PAGES, json={"message": "boom"}, status=status)
    with pytest.raises(RuntimeError, match=f"failed {status}"):
        rec.notion_request("POST", "/pages", notion_settings)
    assert len(http.calls) == 1


def test_error_message_is_truncated(http, notion_settings):
    http.add(responses.POST, PAGES, body="x" * 5000, status=400)
    with pytest.raises(RuntimeError) as err:
        rec.notion_request("POST", "/pages", notion_settings)
    assert len(str(err.value)) < 400, "a huge HTML error page must not flood the log"


def test_http_date_retry_after_is_tolerated(http, notion_settings):
    """RFC 9110 permits Retry-After to be an HTTP-date. It must not crash the sync."""
    http.add(responses.POST, PAGES, json={}, status=429,
             headers={"Retry-After": "Wed, 21 Oct 2015 07:28:00 GMT"})
    http.add(responses.POST, PAGES, json={"id": "p1"}, status=200)
    assert rec.notion_request("POST", "/pages", notion_settings)["id"] == "p1"


# --------------------------------------------------------------------------
# notion_properties
# --------------------------------------------------------------------------

def test_properties_map_the_event_onto_the_database_schema(notion_settings):
    event = make_event(id="evt-9", camera="door_camera", sub_label="Jericho",
                       start_time=NOW, end_time=NOW + 20, top_score=0.9166)
    props = rec.notion_properties(event, "fregata/events/door_camera/evt-9/manifest.json", 3, notion_settings)

    assert props["Event ID"]["title"][0]["text"]["content"] == "evt-9"
    assert props["Person"]["select"]["name"] == "Jericho"
    assert props["Camera"]["select"]["name"] == "door_camera"
    assert props["Duration (s)"]["number"] == 20.0
    assert props["Segments"]["number"] == 3
    assert props["Score"]["number"] == 0.917
    assert props["Manifest key"]["rich_text"][0]["text"]["content"].endswith("manifest.json")


@pytest.mark.parametrize("sub_label", [None, "", "   ", {"name": "x"}, 42])
def test_unrecognised_faces_get_a_placeholder(sub_label, notion_settings):
    """An unnamed face must still produce a valid select option."""
    props = rec.notion_properties(make_event(sub_label=sub_label), None, 1, notion_settings)
    assert props["Person"]["select"]["name"] == "Unrecognized"


def test_missing_manifest_key_becomes_an_empty_string(notion_settings):
    """Notion rejects a null rich_text content."""
    props = rec.notion_properties(make_event(), None, 0, notion_settings)
    assert props["Manifest key"]["rich_text"][0]["text"]["content"] == ""


def test_score_is_omitted_when_absent(notion_settings):
    assert "Score" not in rec.notion_properties(make_event(top_score=None), None, 1, notion_settings)


def test_zero_score_is_still_reported(notion_settings):
    """0.0 is a real score, not a missing one."""
    props = rec.notion_properties(make_event(top_score=0.0), None, 1, notion_settings)
    assert props["Score"]["number"] == 0.0


def test_seen_is_a_date_range_with_offsets(notion_settings):
    props = rec.notion_properties(make_event(start_time=NOW, end_time=NOW + 20), None, 1, notion_settings)
    date = props["Seen"]["date"]
    assert date["start"] < date["end"]
    assert date["start"][-6] in "+-", "Notion needs an explicit UTC offset"


def test_properties_are_json_serialisable(notion_settings):
    """They go straight into a request body."""
    json.dumps(rec.notion_properties(make_event(sub_label="José"), "k", 2, notion_settings))


# --------------------------------------------------------------------------
# sync_notion — gating
# --------------------------------------------------------------------------

def test_dry_run_never_calls_notion(http, state_db, settings_factory):
    rec.sync_notion(make_event(), "k", 1, state_db,
                    settings_factory(dry_run=True, notion_token="t", notion_database_id="d"))
    assert len(http.calls) == 0


@pytest.mark.parametrize("over", [{"notion_token": None}, {"notion_database_id": None}])
def test_missing_credentials_disable_the_integration(http, state_db, settings_factory, over):
    base = dict(dry_run=False, notion_token="t", notion_database_id="d")
    base.update(over)
    rec.sync_notion(make_event(), "k", 1, state_db, settings_factory(**base))
    assert len(http.calls) == 0


# --------------------------------------------------------------------------
# sync_notion — happy path and idempotency
# --------------------------------------------------------------------------

def test_first_sync_creates_a_page_and_records_it(http, state_db, notion_settings):
    http.add(responses.POST, PAGES, json={"id": "page-1"}, status=200)
    rec.sync_notion(make_event(id="e1"), "k", 2, state_db, notion_settings)

    row = state_db.execute("SELECT * FROM notion_delivery WHERE event_id='e1'").fetchone()
    assert row["page_id"] == "page-1"
    assert row["synced_at"] is not None
    assert row["last_error"] is None


def test_a_synced_event_is_never_pushed_twice(http, state_db, notion_settings):
    http.add(responses.POST, PAGES, json={"id": "page-1"}, status=200)
    event = make_event(id="e1")
    rec.sync_notion(event, "k", 2, state_db, notion_settings)
    rec.sync_notion(event, "k", 2, state_db, notion_settings)
    rec.sync_notion(event, "k", 2, state_db, notion_settings)

    assert len([c for c in http.calls if c.request.url == PAGES]) == 1


def test_a_failed_sync_records_the_error_without_raising(http, state_db, notion_settings):
    http.add(responses.POST, PAGES, json={"message": "unauthorized"}, status=401)
    rec.sync_notion(make_event(id="e1"), "k", 1, state_db, notion_settings)  # must not raise: Notion is a secondary sink

    row = state_db.execute("SELECT * FROM notion_delivery WHERE event_id='e1'").fetchone()
    assert row["synced_at"] is None
    assert "401" in row["last_error"]


def test_retry_after_failure_searches_before_creating(http, state_db, notion_settings):
    """The interesting case: the first attempt may have created a page before
    the connection dropped, so a blind re-POST would duplicate it."""
    http.add(responses.POST, PAGES, json={"message": "boom"}, status=500)
    rec.sync_notion(make_event(id="e1"), "k", 1, state_db, notion_settings)  # must not raise: Notion is a secondary sink

    http.add(responses.POST, f"{rec.NOTION_API}/databases/db123/query",
             json={"results": [{"id": "already-there"}]}, status=200)
    rec.sync_notion(make_event(id="e1"), "k", 1, state_db, notion_settings)

    row = state_db.execute("SELECT * FROM notion_delivery WHERE event_id='e1'").fetchone()
    assert row["page_id"] == "already-there"
    assert row["synced_at"] is not None
    assert len([c for c in http.calls if c.request.url == PAGES]) == 1, "must not re-create"


def test_retry_creates_the_page_when_the_search_finds_nothing(http, state_db, notion_settings):
    http.add(responses.POST, PAGES, json={"message": "boom"}, status=500)
    rec.sync_notion(make_event(id="e1"), "k", 1, state_db, notion_settings)  # must not raise: Notion is a secondary sink

    http.add(responses.POST, f"{rec.NOTION_API}/databases/db123/query",
             json={"results": []}, status=200)
    http.add(responses.POST, PAGES, json={"id": "page-2"}, status=200)
    rec.sync_notion(make_event(id="e1"), "k", 1, state_db, notion_settings)

    row = state_db.execute("SELECT page_id FROM notion_delivery WHERE event_id='e1'").fetchone()
    assert row["page_id"] == "page-2"


def test_error_is_cleared_once_the_sync_succeeds(http, state_db, notion_settings):
    http.add(responses.POST, PAGES, json={"message": "boom"}, status=500)
    rec.sync_notion(make_event(id="e1"), "k", 1, state_db, notion_settings)  # must not raise: Notion is a secondary sink

    http.add(responses.POST, f"{rec.NOTION_API}/databases/db123/query", json={"results": []}, status=200)
    http.add(responses.POST, PAGES, json={"id": "p"}, status=200)
    rec.sync_notion(make_event(id="e1"), "k", 1, state_db, notion_settings)

    assert state_db.execute(
        "SELECT last_error FROM notion_delivery WHERE event_id='e1'").fetchone()["last_error"] is None


def test_the_dedupe_query_filters_on_the_event_id(http, state_db, notion_settings):
    http.add(responses.POST, PAGES, json={"message": "boom"}, status=500)
    rec.sync_notion(make_event(id="e1"), "k", 1, state_db, notion_settings)  # must not raise: Notion is a secondary sink

    http.add(responses.POST, f"{rec.NOTION_API}/databases/db123/query", json={"results": []}, status=200)
    http.add(responses.POST, PAGES, json={"id": "p"}, status=200)
    rec.sync_notion(make_event(id="e1"), "k", 1, state_db, notion_settings)

    query = [c for c in http.calls if c.request.url.endswith("/query")][0]
    assert json.loads(query.request.body)["filter"] == {
        "property": "Event ID", "title": {"equals": "e1"}}


@pytest.mark.defect
@pytest.mark.xfail(strict=True, reason=(
    "Dedupe depends entirely on the local state DB. Lose it — which "
    ".env.example warns happens merely by running from another directory, "
    "since STATE_DB_PATH defaults to a relative path — and every past event is "
    "re-pushed as a duplicate Notion page. The search-before-create guard is "
    "skipped precisely when there is no row."))
def test_losing_the_state_db_does_not_duplicate_pages(http, state_db, notion_settings, tmp_path):
    http.add(responses.POST, PAGES, json={"id": "page-1"}, status=200)
    event = make_event(id="e1")
    rec.sync_notion(event, "k", 1, state_db, notion_settings)

    fresh = rec.open_state(tmp_path / "rebuilt-state.db")   # state lost, program restarted
    try:
        http.add(responses.POST, PAGES, json={"id": "page-2-DUPLICATE"}, status=200)
        rec.sync_notion(event, "k", 1, fresh, notion_settings)
        assert len([c for c in http.calls if c.request.url == PAGES]) == 1
    finally:
        fresh.close()


# --------------------------------------------------------------------------
# sync_notion — terminal state, privacy, and API-version tolerance
# --------------------------------------------------------------------------

def test_a_persistently_failing_event_is_eventually_given_up_on(http, state_db, settings_factory):
    """Without a terminal state, 312 dead events would be retried every 30s forever,
    ahead of the person actually standing at the door."""
    s = settings_factory(dry_run=False, notion_token="t", notion_database_id="db123",
                         notion_max_attempts=3)
    http.add(responses.POST, PAGES, json={"message": "nope"}, status=400)
    http.add(responses.POST, f"{rec.NOTION_API}/databases/db123/query", json={"results": []}, status=200)

    for _ in range(6):
        rec.sync_notion(make_event(id="e1"), "k", 1, state_db, s)

    row = state_db.execute("SELECT attempts,synced_at FROM notion_delivery WHERE event_id='e1'").fetchone()
    assert row["attempts"] == 3, "must stop counting at the limit"
    assert row["synced_at"] is None
    assert len([c for c in http.calls if c.request.url == PAGES]) == 3, "no calls after giving up"


def test_a_dropped_connection_leaves_a_row_but_costs_no_attempt(http, state_db, notion_settings):
    """The row must exist so the next pass dedupes before re-creating. But a dropped
    connection says nothing about this event, so it must not count toward giving up."""
    http.add(responses.POST, PAGES, body=Exception("connection dropped"))
    rec.sync_notion(make_event(id="e1"), "k", 1, state_db, notion_settings)

    row = state_db.execute("SELECT attempts,last_error FROM notion_delivery WHERE event_id='e1'").fetchone()
    assert row is not None, "no row means the next pass would create a duplicate page"
    assert row["attempts"] == 0, "a transient failure must be refunded"
    assert row["last_error"] is not None


def test_an_unreachable_workspace_costs_no_attempts(http, state_db, notion_settings):
    """A wrong NOTION_DATABASE_ID or an unshared integration is global, not per-event.
    Charging it would retire the whole backlog after a few minutes of misconfiguration."""
    http.replace(responses.GET, f"{rec.NOTION_API}/databases/db123",
                 json={"message": "not found"}, status=404)
    for _ in range(8):                      # more polls than notion_max_attempts
        rec.sync_notion(make_event(id="e1"), "k", 1, state_db, notion_settings)

    row = state_db.execute("SELECT attempts,synced_at FROM notion_delivery WHERE event_id='e1'").fetchone()
    assert row["attempts"] == 0, "the event must still be syncable once the config is fixed"
    assert row["synced_at"] is None

    # Operator fixes the database id; the very next pass must succeed.
    http.replace(responses.GET, f"{rec.NOTION_API}/databases/db123",
                 json={"data_sources": []}, status=200)
    http.add(responses.POST, PAGES, json={"id": "p1"}, status=200)
    rec.sync_notion(make_event(id="e1"), "k", 1, state_db, notion_settings)
    assert state_db.execute(
        "SELECT synced_at FROM notion_delivery WHERE event_id='e1'").fetchone()["synced_at"] is not None


def test_a_rejected_page_does_cost_an_attempt(http, state_db, notion_settings):
    """A 400 on /pages means Notion rejected THIS page — that is the event's fault."""
    http.add(responses.POST, PAGES, json={"message": "validation_error"}, status=400)
    rec.sync_notion(make_event(id="e1"), "k", 1, state_db, notion_settings)

    assert state_db.execute(
        "SELECT attempts FROM notion_delivery WHERE event_id='e1'").fetchone()["attempts"] == 1


def test_person_is_withheld_unless_explicitly_enabled(settings_factory):
    """sub_label is a real person's name. Publishing it to Notion is opt-in."""
    off = settings_factory(notion_include_person=False)
    on = settings_factory(notion_include_person=True)
    event = make_event(sub_label="Jericho")

    assert rec.notion_properties(event, "k", 1, off)["Person"]["select"]["name"] == "Unrecognized"
    assert rec.notion_properties(event, "k", 1, on)["Person"]["select"]["name"] == "Jericho"


def test_a_comma_in_a_name_is_stripped(settings_factory):
    """Notion rejects a comma in a select option, which would fail every run forever."""
    s = settings_factory(notion_include_person=True)
    props = rec.notion_properties(make_event(sub_label="Del Rosario, Jericho"), "k", 1, s)
    assert "," not in props["Person"]["select"]["name"]


def test_a_data_source_workspace_uses_the_new_parent_and_query_path(http, state_db, notion_settings):
    """Notion 2025-09-03 split databases into data sources. Resolve, don't assume."""
    http.replace(responses.GET, f"{rec.NOTION_API}/databases/db123",
                 json={"data_sources": [{"id": "ds-9"}]}, status=200)
    http.add(responses.POST, PAGES, json={"id": "p1"}, status=200)
    rec.sync_notion(make_event(id="e1"), "k", 1, state_db, notion_settings)

    body = json.loads([c for c in http.calls if c.request.url == PAGES][0].request.body)
    assert body["parent"] == {"data_source_id": "ds-9"}


def test_clip_url_is_written_when_a_public_base_url_is_set(settings_factory):
    """A stable link, not a presigned one — clipserver resolves it at click time."""
    s = settings_factory(public_base_url="https://mac-mini.tail1234.ts.net")
    props = rec.notion_properties(make_event(id="1700000000.0-abc123"), "k", 3, s)
    assert props["Clip"]["url"] == "https://mac-mini.tail1234.ts.net/clip/1700000000.0-abc123"
    assert "X-Amz-Signature" not in props["Clip"]["url"], "must not bake in a signature"


def test_clip_is_omitted_without_a_public_base_url(settings_factory):
    """No tailnet, no Clip column — behaviour is unchanged for everyone else."""
    props = rec.notion_properties(make_event(), "k", 1, settings_factory(public_base_url=None))
    assert "Clip" not in props


def test_a_trailing_slash_on_the_base_url_does_not_double_up(settings_factory, monkeypatch):
    monkeypatch.setenv("PUBLIC_BASE_URL", "https://mac-mini.tail1234.ts.net/")
    monkeypatch.setenv("S3_BUCKET", "b")
    assert rec.Settings.from_env().public_base_url == "https://mac-mini.tail1234.ts.net"


# --------------------------------------------------------------------------
# backfill_clip — patching the Clip link onto already-synced pages
# --------------------------------------------------------------------------

CLIP_BASE = "https://mac-mini.tail1234.ts.net"


@pytest.fixture
def clip_settings(settings_factory):
    """Same workspace as notion_settings, but with a clipserver configured."""
    def _make(**over):
        base = dict(dry_run=False, notion_token="secret_tok", notion_database_id="db123",
                    notion_version="2026-03-11", public_base_url=CLIP_BASE)
        base.update(over)
        return settings_factory(**base)
    return _make


def patches(http):
    return [c for c in http.calls if c.request.method == "PATCH"]


def clip_row(state_db, event_id="e1"):
    return state_db.execute(
        "SELECT page_id,synced_at,clip_synced_at,clip_attempts,last_error"
        " FROM notion_delivery WHERE event_id=?", (event_id,)).fetchone()


def sync_page_without_clip(http, state_db, notion_settings, event_id="e1", page_id="page-1"):
    """Create a page the way the pre-clip-links build did: synced, no Clip column.

    This is exactly the state of the 312-event backlog — synced_at set,
    clip_synced_at NULL — reproduced through the real creation path so the
    production SQL writes the row, not the test.
    """
    http.add(responses.POST, PAGES, json={"id": page_id}, status=200)
    rec.sync_notion(make_event(id=event_id), "k", 1, state_db, notion_settings)
    row = clip_row(state_db, event_id)
    assert row["synced_at"] is not None and row["clip_synced_at"] is None
    return row


def test_backfill_patches_the_clip_url_onto_a_synced_page(http, state_db, notion_settings, clip_settings):
    """The backlog case: page synced before PUBLIC_BASE_URL existed, then the
    operator configures the clipserver. The next pass must PATCH exactly once."""
    sync_page_without_clip(http, state_db, notion_settings)

    http.add(responses.PATCH, f"{rec.NOTION_API}/pages/page-1", json={"id": "page-1"}, status=200)
    rec.sync_notion(make_event(id="e1"), "k", 1, state_db, clip_settings())

    sent = patches(http)
    assert len(sent) == 1
    assert sent[0].request.url == f"{rec.NOTION_API}/pages/page-1"
    assert json.loads(sent[0].request.body) == {
        "properties": {"Clip": {"url": f"{CLIP_BASE}/clip/e1"}}}
    row = clip_row(state_db)
    assert row["clip_synced_at"] is not None
    assert row["last_error"] is None

    # Idempotency: the clip is recorded as delivered, so the next pass is silent.
    rec.sync_notion(make_event(id="e1"), "k", 1, state_db, clip_settings())
    assert len(patches(http)) == 1, "a backfilled page must never be patched twice"


def test_no_patch_is_ever_sent_without_a_public_base_url(http, state_db, notion_settings):
    """No clipserver, no Clip column — and crucially no attempt charged, so the
    backlog stays eligible for the day PUBLIC_BASE_URL is finally set."""
    sync_page_without_clip(http, state_db, notion_settings)

    for _ in range(3):
        rec.sync_notion(make_event(id="e1"), "k", 1, state_db, notion_settings)

    assert patches(http) == []
    row = clip_row(state_db)
    assert row["clip_synced_at"] is None
    assert row["clip_attempts"] == 0


def test_a_rejected_patch_goes_terminal_without_touching_synced_at(http, state_db, notion_settings, clip_settings):
    """A Notion DB with no Clip property 400s on EVERY patch. That must go
    terminal after notion_max_attempts, not hammer Notion every 30s poll —
    and the page itself stays synced throughout: only the clip failed."""
    sync_page_without_clip(http, state_db, notion_settings)
    s = clip_settings(notion_max_attempts=3)

    http.add(responses.PATCH, f"{rec.NOTION_API}/pages/page-1",
             json={"message": "Clip is not a property that exists"}, status=400)
    for _ in range(6):                        # more polls than the budget allows
        rec.sync_notion(make_event(id="e1"), "k", 1, state_db, s)

    assert len(patches(http)) == 3, "no PATCHes after giving up"
    row = clip_row(state_db)
    assert row["clip_attempts"] == 3, "must stop counting at the limit"
    assert row["clip_synced_at"] is None
    assert row["synced_at"] is not None, "a clip failure must never unsync the page"
    assert "400" in row["last_error"]


def test_a_5xx_patch_is_refunded_and_retried(http, state_db, notion_settings, clip_settings):
    """A 500 says nothing about this page — Notion was having a moment. The
    attempt must be refunded so an outage cannot retire the clip backlog."""
    sync_page_without_clip(http, state_db, notion_settings)

    http.add(responses.PATCH, f"{rec.NOTION_API}/pages/page-1", json={"message": "boom"}, status=500)
    rec.sync_notion(make_event(id="e1"), "k", 1, state_db, clip_settings())

    row = clip_row(state_db)
    assert row["clip_attempts"] == 0, "a transient failure must be refunded"
    assert row["clip_synced_at"] is None
    assert row["last_error"] is not None

    # Notion recovers; the very next pass must succeed.
    http.replace(responses.PATCH, f"{rec.NOTION_API}/pages/page-1", json={"id": "page-1"}, status=200)
    rec.sync_notion(make_event(id="e1"), "k", 1, state_db, clip_settings())
    assert clip_row(state_db)["clip_synced_at"] is not None


def test_a_dropped_connection_on_patch_is_refunded_and_retried(http, state_db, notion_settings, clip_settings):
    sync_page_without_clip(http, state_db, notion_settings)

    http.add(responses.PATCH, f"{rec.NOTION_API}/pages/page-1", body=Exception("connection dropped"))
    rec.sync_notion(make_event(id="e1"), "k", 1, state_db, clip_settings())

    row = clip_row(state_db)
    assert row["clip_attempts"] == 0, "the network is not this event's fault"
    assert row["clip_synced_at"] is None

    http.add(responses.PATCH, f"{rec.NOTION_API}/pages/page-1", json={"id": "page-1"}, status=200)
    rec.sync_notion(make_event(id="e1"), "k", 1, state_db, clip_settings())
    assert clip_row(state_db)["clip_synced_at"] is not None


def test_a_page_created_with_a_base_url_needs_no_backfill(http, state_db, clip_settings):
    """notion_properties already put the Clip column into the created page, so
    clip_synced_at is stamped at creation and no pointless PATCH follows."""
    s = clip_settings()
    http.add(responses.POST, PAGES, json={"id": "page-1"}, status=200)
    rec.sync_notion(make_event(id="e1"), "k", 1, state_db, s)

    assert clip_row(state_db)["clip_synced_at"] is not None

    rec.sync_notion(make_event(id="e1"), "k", 1, state_db, s)
    assert patches(http) == [], "the clip arrived with the page; nothing to patch"


def test_an_old_state_db_gains_the_clip_columns(tmp_path):
    """A state DB written before clip links existed must migrate in place, and
    its rows must come out backfill-eligible (clip_synced_at NULL, 0 attempts)."""
    db = tmp_path / "old-state.db"
    conn = sqlite3.connect(db)
    conn.execute("""CREATE TABLE notion_delivery (
                      event_id TEXT PRIMARY KEY,
                      page_id TEXT,
                      synced_at REAL,
                      last_error TEXT,
                      attempts INTEGER NOT NULL DEFAULT 0,
                      updated_at REAL NOT NULL)""")
    conn.execute("INSERT INTO notion_delivery(event_id,page_id,synced_at,updated_at)"
                 " VALUES('e1','page-1',1.0,1.0)")
    conn.commit()
    conn.close()

    state = rec.open_state(db)
    try:
        assert {"clip_synced_at", "clip_attempts"} <= rec.table_columns(state, "notion_delivery")
        row = state.execute("SELECT clip_synced_at,clip_attempts FROM notion_delivery"
                            " WHERE event_id='e1'").fetchone()
        assert row["clip_synced_at"] is None, "old rows are exactly the backlog to backfill"
        assert row["clip_attempts"] == 0
    finally:
        state.close()


def insert_synced_row_without_page_id(state_db, event_id="e1"):
    # A synced row should always have its page_id, but a crash between the page
    # POST and the success UPDATE (or a hand-edited DB) can leave it NULL.
    state_db.execute(
        "INSERT INTO notion_delivery(event_id,page_id,synced_at,attempts,updated_at)"
        " VALUES(?,NULL,?,1,?)", (event_id, NOW, NOW))
    state_db.commit()


def test_a_missing_page_id_is_recovered_through_the_dedupe_query(http, state_db, clip_settings):
    insert_synced_row_without_page_id(state_db)
    http.add(responses.POST, f"{rec.NOTION_API}/databases/db123/query",
             json={"results": [{"id": "page-77"}]}, status=200)
    http.add(responses.PATCH, f"{rec.NOTION_API}/pages/page-77", json={"id": "page-77"}, status=200)

    rec.sync_notion(make_event(id="e1"), "k", 1, state_db, clip_settings())

    query = [c for c in http.calls if c.request.url.endswith("/query")][0]
    assert json.loads(query.request.body)["filter"] == {
        "property": "Event ID", "title": {"equals": "e1"}}
    assert len(patches(http)) == 1
    row = clip_row(state_db)
    assert row["clip_synced_at"] is not None
    assert row["page_id"] == "page-77", "the recovered page_id must be persisted"


def test_a_synced_row_whose_page_cannot_be_found_is_charged(http, state_db, clip_settings):
    """The lookup RAN and found nothing: this row will never succeed, so the
    attempt counts — otherwise it queries Notion on every poll forever."""
    insert_synced_row_without_page_id(state_db)
    http.add(responses.POST, f"{rec.NOTION_API}/databases/db123/query",
             json={"results": []}, status=200)

    rec.sync_notion(make_event(id="e1"), "k", 1, state_db, clip_settings())

    row = clip_row(state_db)
    assert row["clip_attempts"] == 1
    assert row["clip_synced_at"] is None
    assert row["last_error"] is not None
    assert patches(http) == [], "nothing to patch without a page"


def test_a_failed_page_lookup_is_not_charged(http, state_db, clip_settings):
    """The lookup itself failing is the workspace or the network, not this
    event — same blame semantics as the creation path's target resolution."""
    insert_synced_row_without_page_id(state_db)
    http.replace(responses.GET, f"{rec.NOTION_API}/databases/db123",
                 json={"message": "not found"}, status=404)

    for _ in range(8):                      # more polls than notion_max_attempts
        rec.sync_notion(make_event(id="e1"), "k", 1, state_db, clip_settings())

    row = clip_row(state_db)
    assert row["clip_attempts"] == 0, "still eligible once the config is fixed"
    assert row["last_error"] is not None


def test_status_reports_the_clip_backfill_counters(settings_factory, capsys):
    settings = settings_factory(notion_max_attempts=3)
    state = rec.open_state(settings.state_db)
    rows = [
        ("done", "p1", NOW, NOW, 0),        # clip delivered
        ("pending", "p2", NOW, None, 1),    # synced, clip still owed, budget left
        ("dead", "p3", NOW, None, 3),       # synced, clip gave up at the limit
        ("unsynced", None, None, None, 0),  # no page yet: not backfill's problem
    ]
    state.executemany(
        "INSERT INTO notion_delivery(event_id,page_id,synced_at,clip_synced_at,clip_attempts,updated_at)"
        " VALUES(?,?,?,?,?,1.0)", rows)
    state.commit()
    state.close()

    assert rec.status(settings) == 0
    out = json.loads(capsys.readouterr().out)
    assert out["clip_synced"] == 1
    assert out["clip_pending"] == 1
    assert out["clip_gave_up"] == 1
