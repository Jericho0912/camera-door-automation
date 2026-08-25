"""Clip links: a presigned viewer page in S3, its URL kept fresh in Notion.

The refresh pass is driven by the real state DB and a moto-backed S3, with Notion
mocked at the HTTP layer — so the SQL, the presigning, and the PATCH bodies are the
production ones. The property the whole design rests on: the Notion link is a plain
bearer URL that the poll loop re-signs before it expires, with the same blame/budget
temperament as page creation, except the budget refills on success because refresh
recurs forever.
"""
from __future__ import annotations

import json
import time

import pytest
import requests
import responses

import reconciler as rec
from conftest import NOW

EID = "1700000000.0-abc123"
PAGES = f"{rec.NOTION_API}/pages"
DAY = 86_400


@pytest.fixture
def http():
    with responses.RequestsMock(assert_all_requests_are_fired=False) as m:
        # Only the page_id-recovery path resolves notion_targets; an empty data_sources
        # list falls back to the pre-2025-09-03 database_id shape, like test_notion.py.
        m.add(responses.GET, f"{rec.NOTION_API}/databases/db123",
              json={"data_sources": []}, status=200)
        yield m


@pytest.fixture(autouse=True)
def reset_notion_targets():
    rec._NOTION_TARGETS = None
    yield
    rec._NOTION_TARGETS = None


@pytest.fixture(autouse=True)
def no_sleep(monkeypatch):
    """Neither PATCH pacing nor 429 backoff may add real seconds to the suite."""
    monkeypatch.setattr(rec.time, "sleep", lambda s: None)


@pytest.fixture(autouse=True)
def no_cred_probe(monkeypatch):
    """The session-token warning probes boto3's credential chain; keep tests hermetic."""
    monkeypatch.setattr(rec, "warn_if_presign_capped", lambda settings: None)


@pytest.fixture
def clip_settings(settings_factory):
    return settings_factory(dry_run=False, notion_token="secret_tok",
                            notion_database_id="db123", clip_links=True)


def seed(state, s3=None, event_id=EID, camera="door_camera", page_id="p1",
         synced=True, clip_signed_at=None, clip_attempts=0, nsegs=3, completed=True):
    """A delivered event with a synced Notion page, eligible for a clip link."""
    keys = []
    for i in range(nsegs):
        k = f"fregata/recordings/{camera}/{event_id}-{i:02d}.mp4"
        keys.append(k)
        if s3 is not None:
            s3.put_object(Bucket="test-bucket", Key=k, Body=b"\x00mp4")
        state.execute(
            "INSERT INTO segment_delivery(event_id,source_path,s3_key,etag,uploaded_at) VALUES(?,?,?,?,?)",
            (event_id, f"/local/{event_id}-{i}.mp4", k, "e", NOW))
    state.execute(
        "INSERT INTO event_delivery(event_id,camera,start_time,end_time,completed_at,updated_at)"
        " VALUES(?,?,?,?,?,?)",
        (event_id, camera, NOW, NOW + 20, NOW if completed else None, NOW))
    state.execute(
        "INSERT INTO notion_delivery(event_id,page_id,synced_at,clip_signed_at,clip_attempts,updated_at)"
        " VALUES(?,?,?,?,?,?)",
        (event_id, page_id, NOW if synced else None, clip_signed_at, clip_attempts, NOW))
    state.commit()
    return keys


def patches(http):
    return [c for c in http.calls if c.request.method == "PATCH"]


def clip_row(state, event_id=EID):
    return state.execute("SELECT * FROM notion_delivery WHERE event_id=?", (event_id,)).fetchone()


# --------------------------------------------------------------------------
# the happy path
# --------------------------------------------------------------------------

def test_a_never_linked_page_gets_a_presigned_viewer_url(http, s3, state_db, clip_settings):
    seed(state_db, s3, clip_attempts=2)
    http.add(responses.PATCH, f"{PAGES}/p1", json={"id": "p1"}, status=200)

    rec.refresh_clip_links(state_db, s3, clip_settings)

    (call,) = patches(http)
    url = json.loads(call.request.body)["properties"]["Clip"]["url"]
    assert f"/events/door_camera/{EID}/index.html" in url
    assert "X-Amz-Signature" in url, "the link must carry its own grant"
    row = clip_row(state_db)
    assert row["clip_signed_at"] is not None
    assert row["clip_attempts"] == 0, "success must refill the budget — refresh recurs forever"
    assert row["last_error"] is None


def test_the_viewer_page_plays_every_delivered_segment_and_only_those(http, s3, state_db, clip_settings):
    keys = seed(state_db, s3)
    # a segment that never uploaded must not be offered
    state_db.execute(
        "INSERT INTO segment_delivery(event_id,source_path,s3_key,etag,uploaded_at) VALUES(?,?,?,?,?)",
        (EID, "/local/pending.mp4", "fregata/recordings/door_camera/pending.mp4", None, None))
    state_db.commit()
    http.add(responses.PATCH, f"{PAGES}/p1", json={"id": "p1"}, status=200)

    rec.refresh_clip_links(state_db, s3, clip_settings)

    obj = s3.get_object(Bucket="test-bucket", Key=f"fregata/events/door_camera/{EID}/index.html")
    assert obj["ContentType"].startswith("text/html")
    assert obj["CacheControl"] == "no-store"
    body = obj["Body"].read().decode()
    assert body.count("<video") == len(keys) == 3
    assert "pending.mp4" not in body
    assert "X-Amz-Signature" in body, "each video src is its own presigned grant"


def test_page_creation_never_writes_clip_and_refresh_does(http, s3, pipeline, segment_file):
    """Single-writer: the creation path stays clip-free, the refresh pass owns the column."""
    from conftest import make_event, make_recording
    segment_file("door_camera/seg-1.mp4")
    http.add(responses.POST, PAGES, json={"id": "p77"}, status=200)
    http.add(responses.PATCH, f"{PAGES}/p77", json={"id": "p77"}, status=200)
    w = pipeline(events=[make_event(id=EID)], recordings=[make_recording()],
                 dry_run=False, notion_token="secret_tok", notion_database_id="db123",
                 clip_links=True)

    w.run(w.events()[0])
    created = json.loads([c for c in http.calls if c.request.url == PAGES][0].request.body)
    assert "Clip" not in created["properties"]

    rec.refresh_clip_links(w.state, w.client, w.settings)
    (call,) = patches(http)
    assert "X-Amz-Signature" in json.loads(call.request.body)["properties"]["Clip"]["url"]


# --------------------------------------------------------------------------
# eligibility: who gets signed, and in what order
# --------------------------------------------------------------------------

def test_stale_links_are_resigned_and_fresh_ones_left_alone(http, s3, state_db, clip_settings):
    seed(state_db, s3, event_id="stale-1", page_id="pS", clip_signed_at=time.time() - 6 * DAY)
    seed(state_db, s3, event_id="fresh-1", page_id="pF", clip_signed_at=time.time() - 1 * DAY)
    http.add(responses.PATCH, f"{PAGES}/pS", json={"id": "pS"}, status=200)

    rec.refresh_clip_links(state_db, s3, clip_settings)

    (call,) = patches(http)
    assert call.request.url.endswith("/pS")


def test_never_linked_rows_beat_routine_refreshes(http, s3, state_db, clip_settings, monkeypatch):
    """NULL sorts before every real timestamp ASC, so the backlog and brand-new pages win."""
    monkeypatch.setattr(rec, "CLIP_REFRESH_BATCH", 1)
    seed(state_db, s3, event_id="stale-1", page_id="pS", clip_signed_at=time.time() - 30 * DAY)
    seed(state_db, s3, event_id="new-1", page_id="pN", clip_signed_at=None)
    http.add(responses.PATCH, f"{PAGES}/pN", json={"id": "pN"}, status=200)

    rec.refresh_clip_links(state_db, s3, clip_settings)

    (call,) = patches(http)
    assert call.request.url.endswith("/pN")


def test_the_batch_is_capped_per_pass(http, s3, state_db, clip_settings, monkeypatch):
    monkeypatch.setattr(rec, "CLIP_REFRESH_BATCH", 3)
    for i in range(5):
        seed(state_db, s3, event_id=f"e{i}", page_id=f"p{i}")
        http.add(responses.PATCH, f"{PAGES}/p{i}", json={"id": f"p{i}"}, status=200)

    rec.refresh_clip_links(state_db, s3, clip_settings)
    assert len(patches(http)) == 3, "the heal must be paced, not a thundering herd"


@pytest.mark.parametrize("over", [
    dict(synced=False),        # no page yet: the creation path's problem
    dict(completed=False),     # footage not fully in S3 yet: nothing to link
    dict(clip_attempts=5),     # budget exhausted: silence, not hammering
])
def test_ineligible_rows_are_never_touched(http, s3, state_db, clip_settings, over):
    seed(state_db, s3, **over)
    rec.refresh_clip_links(state_db, s3, clip_settings)
    assert patches(http) == []


def test_disabled_and_dry_run_do_nothing_at_all(http, s3, state_db, settings_factory):
    seed(state_db, s3)
    for settings in (
        settings_factory(dry_run=False, notion_token="t", notion_database_id="d", clip_links=False),
        settings_factory(dry_run=True, notion_token="t", notion_database_id="d", clip_links=True),
    ):
        rec.refresh_clip_links(state_db, s3, settings)
    assert len(http.calls) == 0
    keys = [o["Key"] for o in s3.list_objects_v2(Bucket="test-bucket").get("Contents", [])]
    assert not any(k.endswith("index.html") for k in keys)


# --------------------------------------------------------------------------
# the budget: what is charged, what is refunded, what is terminal
# --------------------------------------------------------------------------

def test_a_schema_400_charges_until_terminal_then_goes_quiet(http, s3, state_db, clip_settings):
    """The missing-Clip-property failure: budgeted noise, then silence — never forever."""
    seed(state_db, s3)
    http.add(responses.PATCH, f"{PAGES}/p1",
             json={"message": "Clip is not a property that exists"}, status=400)

    for expected in (1, 2, 3, 4, 5, 5):
        rec.refresh_clip_links(state_db, s3, clip_settings)
        assert clip_row(state_db)["clip_attempts"] == expected
    assert len(patches(http)) == 5
    assert "not a property" in clip_row(state_db)["last_error"]
    assert clip_row(state_db)["clip_signed_at"] is None


@pytest.mark.parametrize("status", [401, 403, 429, 500, 502, 503])
def test_service_trouble_is_refunded(http, s3, state_db, clip_settings, status):
    """401/403 included: a rotated token or unshared integration is the workspace's
    problem, and must not burn the backlog's budget page by page."""
    seed(state_db, s3, clip_attempts=2)
    http.add(responses.PATCH, f"{PAGES}/p1", json={}, status=status)

    rec.refresh_clip_links(state_db, s3, clip_settings)

    row = clip_row(state_db)
    assert row["clip_attempts"] == 2, "a 401/403/429/5xx says nothing about this page"
    assert row["clip_signed_at"] is None
    assert row["last_error"]


def test_a_service_failure_aborts_the_pass(http, s3, state_db, clip_settings):
    """During an outage every row fails identically, each burning real seconds while
    the next pass's deliveries wait — one refunded failure ends the batch."""
    for i in range(3):
        seed(state_db, s3, event_id=f"e{i}", page_id=f"p{i}")
        http.add(responses.PATCH, f"{PAGES}/p{i}", json={}, status=503)

    rec.refresh_clip_links(state_db, s3, clip_settings)
    assert len(patches(http)) == 1, "the remaining rows must be deferred to the next poll"


def test_a_page_level_failure_does_not_abort_the_pass(http, s3, state_db, clip_settings):
    """One deleted page or bad property is that page's problem; its neighbours still refresh."""
    for i in range(3):
        seed(state_db, s3, event_id=f"e{i}", page_id=f"p{i}")
        http.add(responses.PATCH, f"{PAGES}/p{i}", json={}, status=400)

    rec.refresh_clip_links(state_db, s3, clip_settings)
    assert len(patches(http)) == 3


def test_patches_are_paced(http, s3, state_db, clip_settings, monkeypatch):
    slept = []
    monkeypatch.setattr(rec.time, "sleep", lambda s: slept.append(s))
    for i in range(3):
        seed(state_db, s3, event_id=f"e{i}", page_id=f"p{i}")
        http.add(responses.PATCH, f"{PAGES}/p{i}", json={"id": f"p{i}"}, status=200)

    rec.refresh_clip_links(state_db, s3, clip_settings)
    assert slept == [rec.CLIP_PATCH_SPACING] * 2, "spacing between rows, none before the first"


def test_a_kill_mid_refresh_leaves_a_diagnosis(http, s3, state_db, clip_settings, monkeypatch):
    """SIGKILL/power loss between the pre-charge and the outcome cannot refund; at the
    budget boundary that retires the row, so the charge must carry its own explanation."""
    seed(state_db, s3, clip_attempts=4)

    def die(*a, **k):
        raise KeyboardInterrupt
    monkeypatch.setattr(rec, "notion_request", die)

    with pytest.raises(KeyboardInterrupt):
        rec.refresh_clip_links(state_db, s3, clip_settings)

    row = clip_row(state_db)
    assert row["clip_attempts"] == 5, "the pre-charge is what a crash leaves behind"
    assert "interrupted mid-flight" in row["last_error"]


def test_a_dropped_connection_is_refunded(http, s3, state_db, clip_settings):
    seed(state_db, s3, clip_attempts=1)
    http.add(responses.PATCH, f"{PAGES}/p1", body=requests.exceptions.ConnectionError("boom"))

    rec.refresh_clip_links(state_db, s3, clip_settings)
    assert clip_row(state_db)["clip_attempts"] == 1


def test_an_s3_failure_is_refunded_and_notion_is_never_called(http, s3, state_db, clip_settings, monkeypatch):
    seed(state_db, s3, clip_attempts=1)

    def broken_put(**kwargs):
        raise RuntimeError("s3 is having a day")
    monkeypatch.setattr(s3, "put_object", broken_put)

    rec.refresh_clip_links(state_db, s3, clip_settings)

    assert patches(http) == [], "no viewer page uploaded means nothing to link"
    row = clip_row(state_db)
    assert row["clip_attempts"] == 1
    assert "having a day" in row["last_error"]


def test_a_synced_row_with_no_delivered_segments_is_charged(http, s3, state_db, clip_settings):
    """It can never produce a working link; retire it with a diagnosis, don't loop."""
    seed(state_db, s3, nsegs=0)
    rec.refresh_clip_links(state_db, s3, clip_settings)
    row = clip_row(state_db)
    assert row["clip_attempts"] == 1
    assert "no delivered segments" in row["last_error"]
    assert patches(http) == []


# --------------------------------------------------------------------------
# page_id recovery — the state row healed for free, as in the creation path
# --------------------------------------------------------------------------

def test_a_lost_page_id_is_recovered_and_healed(http, s3, state_db, clip_settings):
    seed(state_db, s3, page_id=None)
    http.add(responses.POST, f"{rec.NOTION_API}/databases/db123/query",
             json={"results": [{"id": "p9"}]}, status=200)
    http.add(responses.PATCH, f"{PAGES}/p9", json={"id": "p9"}, status=200)

    rec.refresh_clip_links(state_db, s3, clip_settings)

    row = clip_row(state_db)
    assert row["page_id"] == "p9"
    assert row["clip_signed_at"] is not None


def test_no_page_found_is_the_rows_fault(http, s3, state_db, clip_settings):
    seed(state_db, s3, page_id=None)
    http.add(responses.POST, f"{rec.NOTION_API}/databases/db123/query",
             json={"results": []}, status=200)

    rec.refresh_clip_links(state_db, s3, clip_settings)

    row = clip_row(state_db)
    assert row["clip_attempts"] == 1
    assert "no Notion page found" in row["last_error"]
    assert patches(http) == []


def test_a_failing_lookup_charges_nothing(http, s3, state_db, clip_settings):
    seed(state_db, s3, page_id=None)
    http.add(responses.POST, f"{rec.NOTION_API}/databases/db123/query",
             json={}, status=500)

    rec.refresh_clip_links(state_db, s3, clip_settings)

    assert clip_row(state_db)["clip_attempts"] == 0, "the workspace failing is not this event's fault"
    assert patches(http) == []


# --------------------------------------------------------------------------
# the signatures themselves
# --------------------------------------------------------------------------

def test_presigned_urls_are_sigv4_with_the_configured_ttl(s3, settings_factory):
    """Regression home for the s3v4 fix: without signature_version='s3v4' presigning
    can fall back to SigV2, which newer regions reject outright."""
    from urllib.parse import parse_qs, urlparse
    settings = settings_factory(clip_url_ttl=86400)
    url = rec.presign_get(s3, settings, "fregata/recordings/door_camera/x.mp4")
    q = parse_qs(urlparse(url).query)
    assert q["X-Amz-Algorithm"] == ["AWS4-HMAC-SHA256"]
    assert q["X-Amz-Expires"] == ["86400"]
    assert q["X-Amz-Signature"][0]


def test_the_reconcilers_own_client_pins_sigv4(settings_factory):
    client = rec.s3_client(settings_factory())
    assert client.meta.config.signature_version == "s3v4"


def test_render_player_escapes_attribute_context():
    """Presigned URLs contain '&'; S3-served pages cannot carry a CSP, so escaping
    is the only defense the markup gets."""
    page = rec.render_player("evt<&>1", [("a&b.mp4", "https://s3/x?a=1&b=2")])
    assert "evt&lt;&amp;&gt;1" in page
    assert 'src="https://s3/x?a=1&amp;b=2"' in page
    assert "a&amp;b.mp4" in page


def test_the_dedicated_signer_is_used_only_when_configured(settings_factory):
    assert rec.clip_signer_client(settings_factory()) is None
    signer = rec.clip_signer_client(settings_factory(
        clip_aws_access_key_id="AKIAEXAMPLE", clip_aws_secret_access_key="shhh"))
    assert signer is not None
    assert signer.meta.config.signature_version == "s3v4"


# --------------------------------------------------------------------------
# click-time verification — presigning is local and cannot see a wrong region
# --------------------------------------------------------------------------

class _Resp:
    def __init__(self, status):
        self.status_code = status

    def close(self):
        pass


def test_verification_flags_a_dead_link(state_db, monkeypatch):
    monkeypatch.setattr(rec.requests, "get", lambda url, timeout, stream: _Resp(400))
    rec.verify_clip_url("https://s3/x", state_db, "e1")
    row = clip_row(state_db, "e1")
    assert "verification failed: HTTP 400" in row["last_error"]
    assert "AWS_REGION" in row["last_error"], "the message must name the likely cause"


def test_verification_success_records_nothing(state_db, monkeypatch):
    monkeypatch.setattr(rec.requests, "get", lambda url, timeout, stream: _Resp(200))
    rec.verify_clip_url("https://s3/x", state_db, "e1")
    assert clip_row(state_db, "e1") is None


def test_verification_is_silent_when_it_cannot_reach_s3(state_db, monkeypatch):
    def offline(url, timeout, stream):
        raise OSError("no route")
    monkeypatch.setattr(rec.requests, "get", offline)
    rec.verify_clip_url("https://s3/x", state_db, "e1")
    assert clip_row(state_db, "e1") is None, "an offline check must not invent a failure"


# --------------------------------------------------------------------------
# migration — the highest-blast-radius change: an existing install's state DB
# --------------------------------------------------------------------------

def test_an_old_shape_state_db_gains_the_clip_columns(settings_factory):
    import sqlite3
    settings = settings_factory()
    settings.state_db.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(settings.state_db)
    conn.executescript("""
      CREATE TABLE notion_delivery (
        event_id TEXT PRIMARY KEY,
        page_id TEXT,
        synced_at REAL,
        last_error TEXT,
        updated_at REAL NOT NULL
      );
    """)
    conn.execute("INSERT INTO notion_delivery(event_id,page_id,synced_at,updated_at) VALUES('old','p',1,1)")
    conn.commit()
    conn.close()

    state = rec.open_state(settings.state_db)
    try:
        row = state.execute(
            "SELECT attempts, clip_signed_at, clip_attempts FROM notion_delivery WHERE event_id='old'").fetchone()
        # NULL clip_signed_at at 0 attempts is exactly what makes the pre-feature
        # backlog eligible for its first link once CLIP_LINKS is enabled.
        assert row["attempts"] == 0
        assert row["clip_signed_at"] is None
        assert row["clip_attempts"] == 0
    finally:
        state.close()


# --------------------------------------------------------------------------
# status
# --------------------------------------------------------------------------

def test_status_counts_and_lists_clip_state(settings_factory, capsys):
    settings = settings_factory()
    state = rec.open_state(settings.state_db)
    now = time.time()
    rows = [  # (event_id, clip_signed_at, clip_attempts, last_error)
        ("fresh-1", now - 100, 0, None),
        ("stale-1", now - 6 * DAY, 0, None),
        ("never-1", None, 0, None),
        ("gaveup-1", None, 5, "Clip is not a property that exists"),
    ]
    for eid, signed, att, err in rows:
        state.execute(
            "INSERT INTO notion_delivery(event_id,page_id,synced_at,clip_signed_at,clip_attempts,last_error,updated_at)"
            " VALUES(?,?,?,?,?,?,?)", (eid, "p", now, signed, att, err, now))
    state.commit()
    state.close()

    assert rec.status(settings) == 0
    out = capsys.readouterr().out
    counts = json.loads(out[:out.index("\nCLIP FAILED")])
    assert counts["clip_fresh"] == 1
    assert counts["clip_stale"] == 2, "stale and never-linked both still want signing"
    assert counts["clip_gave_up"] == 1
    assert "CLIP FAILED gaveup-1" in out
