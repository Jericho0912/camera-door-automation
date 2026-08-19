"""The clip server: stable links in, short-lived signed URLs out.

Every request is driven through the real BaseHTTPRequestHandler routing against a
real SQLite state DB and a moto-backed S3, so the SQL and the presigning are the
production ones rather than mocks of them.
"""
from __future__ import annotations

import threading
import time
import urllib.error
import urllib.request
from urllib.parse import parse_qs, urlparse

import pytest

import clipserver as cs
import reconciler as rec
from conftest import NOW


@pytest.fixture
def seeded(settings_factory, s3, state_db):
    """A completed event with three delivered segments and a manifest."""
    settings = settings_factory(bucket="test-bucket")
    keys = [f"fregata/recordings/door_camera/0{i}.mp4" for i in (1, 2, 3)]
    for i, k in enumerate(keys):
        s3.put_object(Bucket="test-bucket", Key=k, Body=b"\x00mp4")
        state_db.execute(
            "INSERT INTO segment_delivery(event_id,source_path,s3_key,etag,uploaded_at) VALUES(?,?,?,?,?)",
            ("1700000000.0-abc123", f"/local/0{i}.mp4", k, "e", NOW))
    # one segment that never uploaded — must not appear anywhere
    state_db.execute(
        "INSERT INTO segment_delivery(event_id,source_path,s3_key,etag,uploaded_at) VALUES(?,?,?,?,?)",
        ("1700000000.0-abc123", "/local/pending.mp4", "fregata/recordings/pending.mp4", None, None))
    state_db.execute(
        "INSERT INTO event_delivery(event_id,camera,start_time,end_time,manifest_key,completed_at,updated_at)"
        " VALUES(?,?,?,?,?,?,?)",
        ("1700000000.0-abc123", "door_camera", NOW, NOW + 20, "fregata/events/door_camera/e/manifest.json", NOW, NOW))
    state_db.commit()
    return settings, keys


@pytest.fixture
def serving(seeded, s3, monkeypatch):
    """The real server on an ephemeral loopback port."""
    settings, keys = seeded
    monkeypatch.setattr(cs.Handler, "settings", settings, raising=False)
    monkeypatch.setattr(cs.Handler, "client", s3, raising=False)
    srv = cs.ThreadingHTTPServer(("127.0.0.1", 0), cs.Handler)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    yield f"http://127.0.0.1:{srv.server_address[1]}", keys
    srv.shutdown()
    srv.server_close()


def get(url, redirect=True):
    opener = urllib.request.build_opener()
    if not redirect:
        class NoRedirect(urllib.request.HTTPRedirectHandler):
            def redirect_request(self, *a, **k):
                return None
        opener = urllib.request.build_opener(NoRedirect)
    try:
        with opener.open(url, timeout=10) as r:
            return r.status, r.headers, r.read()
    except urllib.error.HTTPError as e:
        return e.code, e.headers, e.read()


# --------------------------------------------------------------------------
# routing
# --------------------------------------------------------------------------

def test_healthz(serving):
    base, _ = serving
    assert get(f"{base}/healthz")[0] == 200


def test_clip_page_lists_one_player_per_delivered_segment(serving):
    base, keys = serving
    status, _, body = get(f"{base}/clip/1700000000.0-abc123")
    assert status == 200
    assert body.decode().count("<video") == len(keys) == 3
    assert b"pending.mp4" not in body, "a segment that never uploaded must not be offered"


def test_segment_redirects_to_a_signed_url(serving):
    base, _ = serving
    status, headers, _ = get(f"{base}/clip/1700000000.0-abc123/0", redirect=False)
    assert status == 302
    loc = headers["Location"]
    assert "X-Amz-Signature" in loc and "X-Amz-Expires" in loc
    assert "01.mp4" in loc


def test_the_signed_url_is_a_complete_sigv4_grant(seeded, s3):
    """Structure, not a fetch.

    A presigned URL names the real s3.amazonaws.com host, so fetching it with urllib
    escapes moto's in-process interception and hits AWS for real. What this code owes
    is a well-formed signature; whether S3 honours a valid one is AWS's business.
    """
    settings, keys = seeded
    url = cs.presign(s3, settings, keys[0])
    q = parse_qs(urlparse(url).query)

    assert q["X-Amz-Algorithm"] == ["AWS4-HMAC-SHA256"], "SigV4, not the deprecated SigV2"
    assert q["X-Amz-Expires"] == [str(settings.clip_url_ttl)]
    assert "us-east-1/s3/aws4_request" in q["X-Amz-Credential"][0]
    assert q["X-Amz-Signature"][0]
    assert urlparse(url).path.endswith("01.mp4")


def test_manifest_redirects(serving):
    base, _ = serving
    status, headers, _ = get(f"{base}/manifest/1700000000.0-abc123", redirect=False)
    assert status == 302
    assert "manifest.json" in headers["Location"]


# --------------------------------------------------------------------------
# rejection — the server is on a tailnet, not the open internet, but it still
# must not be the weak link
# --------------------------------------------------------------------------

@pytest.mark.parametrize("path", [
    "/clip/nope",                       # not an event id shape
    "/clip/1700000000.0-abc123/9",      # segment out of range
    "/clip/1700000000.0-abc123/x",      # non-numeric index
    "/clip/9999999999.0-missing",       # well-formed but unknown
    "/manifest/9999999999.0-missing",
    "/clip/..%2f..%2fetc%2fpasswd",     # traversal, encoded
    "/clip/1700000000.0-abc123/0/extra",
    "/",
    "/admin",
])
def test_bad_requests_are_404(serving, path):
    base, _ = serving
    assert get(f"{base}{path}", redirect=False)[0] == 404


def test_event_id_pattern_rejects_sql_metacharacters():
    for bad in ["1' OR '1'='1", "../../etc", "abc", "", "1700000000.0-abc/../x"]:
        assert not cs.EVENT_ID.match(bad)
    for good in ["1700000000.0-abc123", "1786809272.368455-x1xezx", "1700000000-aB9"]:
        assert cs.EVENT_ID.match(good)


# --------------------------------------------------------------------------
# the property the whole design rests on
# --------------------------------------------------------------------------

def test_presigned_urls_are_short_lived(seeded, s3):
    """The whole point of the redirect: the Notion link is permanent, the grant is not."""
    settings, keys = seeded
    q = parse_qs(urlparse(cs.presign(s3, settings, keys[0])).query)
    assert int(q["X-Amz-Expires"][0]) <= 900, "a long-lived signature would defeat the design"


def test_state_db_is_opened_read_only(seeded):
    settings, _ = seeded
    conn = cs.open_state_ro(settings.state_db)
    try:
        with pytest.raises(Exception):
            conn.execute("DELETE FROM segment_delivery")
    finally:
        conn.close()


def test_query_strings_are_never_logged(serving, caplog):
    """Signed URLs are credentials; they must not reach the log."""
    base, _ = serving
    with caplog.at_level("INFO"):
        get(f"{base}/clip/1700000000.0-abc123/0", redirect=False)
    time.sleep(0.05)
    assert "X-Amz-Signature" not in caplog.text
