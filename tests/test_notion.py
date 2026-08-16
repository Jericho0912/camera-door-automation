"""Notion sync: HTTP contract, property mapping, and delivery idempotency.

Notion page creation is the one non-idempotent side effect in the program —
POST /pages has no upsert — so the dedupe logic here carries real weight.
"""
from __future__ import annotations

import json

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
        yield m


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


@pytest.mark.defect
@pytest.mark.xfail(strict=True, reason=(
    "RFC 9110 permits Retry-After to be an HTTP-date, not just seconds. "
    "float() on that raises ValueError, turning a routine rate limit into a "
    "hard event failure."))
def test_http_date_retry_after_is_tolerated(http, notion_settings):
    http.add(responses.POST, PAGES, json={}, status=429,
             headers={"Retry-After": "Wed, 21 Oct 2015 07:28:00 GMT"})
    http.add(responses.POST, PAGES, json={"id": "p1"}, status=200)
    assert rec.notion_request("POST", "/pages", notion_settings)["id"] == "p1"


# --------------------------------------------------------------------------
# notion_properties
# --------------------------------------------------------------------------

def test_properties_map_the_event_onto_the_database_schema():
    event = make_event(id="evt-9", camera="door_camera", sub_label="Jericho",
                       start_time=NOW, end_time=NOW + 20, top_score=0.9166)
    props = rec.notion_properties(event, "fregata/events/door_camera/evt-9/manifest.json", 3)

    assert props["Event ID"]["title"][0]["text"]["content"] == "evt-9"
    assert props["Person"]["select"]["name"] == "Jericho"
    assert props["Camera"]["select"]["name"] == "door_camera"
    assert props["Duration (s)"]["number"] == 20.0
    assert props["Segments"]["number"] == 3
    assert props["Score"]["number"] == 0.917
    assert props["Manifest key"]["rich_text"][0]["text"]["content"].endswith("manifest.json")


@pytest.mark.parametrize("sub_label", [None, "", "   ", {"name": "x"}, 42])
def test_unrecognised_faces_get_a_placeholder(sub_label):
    """An unnamed face must still produce a valid select option."""
    props = rec.notion_properties(make_event(sub_label=sub_label), None, 1)
    assert props["Person"]["select"]["name"] == "Unrecognized"


def test_missing_manifest_key_becomes_an_empty_string():
    """Notion rejects a null rich_text content."""
    props = rec.notion_properties(make_event(), None, 0)
    assert props["Manifest key"]["rich_text"][0]["text"]["content"] == ""


def test_score_is_omitted_when_absent():
    assert "Score" not in rec.notion_properties(make_event(top_score=None), None, 1)


def test_zero_score_is_still_reported():
    """0.0 is a real score, not a missing one."""
    props = rec.notion_properties(make_event(top_score=0.0), None, 1)
    assert props["Score"]["number"] == 0.0


def test_seen_is_a_date_range_with_offsets():
    props = rec.notion_properties(make_event(start_time=NOW, end_time=NOW + 20), None, 1)
    date = props["Seen"]["date"]
    assert date["start"] < date["end"]
    assert date["start"][-6] in "+-", "Notion needs an explicit UTC offset"


def test_properties_are_json_serialisable():
    """They go straight into a request body."""
    json.dumps(rec.notion_properties(make_event(sub_label="José"), "k", 2))


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


def test_a_failed_sync_records_the_error_and_reraises(http, state_db, notion_settings):
    http.add(responses.POST, PAGES, json={"message": "unauthorized"}, status=401)
    with pytest.raises(RuntimeError):
        rec.sync_notion(make_event(id="e1"), "k", 1, state_db, notion_settings)

    row = state_db.execute("SELECT * FROM notion_delivery WHERE event_id='e1'").fetchone()
    assert row["synced_at"] is None
    assert "401" in row["last_error"]


def test_retry_after_failure_searches_before_creating(http, state_db, notion_settings):
    """The interesting case: the first attempt may have created a page before
    the connection dropped, so a blind re-POST would duplicate it."""
    http.add(responses.POST, PAGES, json={"message": "boom"}, status=500)
    with pytest.raises(RuntimeError):
        rec.sync_notion(make_event(id="e1"), "k", 1, state_db, notion_settings)

    http.add(responses.POST, f"{rec.NOTION_API}/databases/db123/query",
             json={"results": [{"id": "already-there"}]}, status=200)
    rec.sync_notion(make_event(id="e1"), "k", 1, state_db, notion_settings)

    row = state_db.execute("SELECT * FROM notion_delivery WHERE event_id='e1'").fetchone()
    assert row["page_id"] == "already-there"
    assert row["synced_at"] is not None
    assert len([c for c in http.calls if c.request.url == PAGES]) == 1, "must not re-create"


def test_retry_creates_the_page_when_the_search_finds_nothing(http, state_db, notion_settings):
    http.add(responses.POST, PAGES, json={"message": "boom"}, status=500)
    with pytest.raises(RuntimeError):
        rec.sync_notion(make_event(id="e1"), "k", 1, state_db, notion_settings)

    http.add(responses.POST, f"{rec.NOTION_API}/databases/db123/query",
             json={"results": []}, status=200)
    http.add(responses.POST, PAGES, json={"id": "page-2"}, status=200)
    rec.sync_notion(make_event(id="e1"), "k", 1, state_db, notion_settings)

    row = state_db.execute("SELECT page_id FROM notion_delivery WHERE event_id='e1'").fetchone()
    assert row["page_id"] == "page-2"


def test_error_is_cleared_once_the_sync_succeeds(http, state_db, notion_settings):
    http.add(responses.POST, PAGES, json={"message": "boom"}, status=500)
    with pytest.raises(RuntimeError):
        rec.sync_notion(make_event(id="e1"), "k", 1, state_db, notion_settings)

    http.add(responses.POST, f"{rec.NOTION_API}/databases/db123/query", json={"results": []}, status=200)
    http.add(responses.POST, PAGES, json={"id": "p"}, status=200)
    rec.sync_notion(make_event(id="e1"), "k", 1, state_db, notion_settings)

    assert state_db.execute(
        "SELECT last_error FROM notion_delivery WHERE event_id='e1'").fetchone()["last_error"] is None


def test_the_dedupe_query_filters_on_the_event_id(http, state_db, notion_settings):
    http.add(responses.POST, PAGES, json={"message": "boom"}, status=500)
    with pytest.raises(RuntimeError):
        rec.sync_notion(make_event(id="e1"), "k", 1, state_db, notion_settings)

    http.add(responses.POST, f"{rec.NOTION_API}/databases/db123/query", json={"results": []}, status=200)
    http.add(responses.POST, PAGES, json={"id": "p"}, status=200)
    rec.sync_notion(make_event(id="e1"), "k", 1, state_db, notion_settings)

    query = [c for c in http.calls if "/databases/" in c.request.url][0]
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
