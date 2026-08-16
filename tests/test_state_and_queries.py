"""The local state store and the two source queries."""
from __future__ import annotations

import sqlite3
import time

import pytest

import reconciler as rec
from conftest import NOW, build_source_db, make_event, make_recording


# --------------------------------------------------------------------------
# open_state
# --------------------------------------------------------------------------

def test_open_state_creates_parent_directories(tmp_path):
    target = tmp_path / "deep" / "nested" / "state.db"
    conn = rec.open_state(target)
    try:
        assert target.exists()
    finally:
        conn.close()


def test_open_state_creates_all_three_tables(tmp_path):
    conn = rec.open_state(tmp_path / "state.db")
    try:
        names = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        assert {"event_delivery", "segment_delivery", "notion_delivery"} <= names
    finally:
        conn.close()


def test_open_state_is_idempotent(tmp_path):
    """Reopened on every pass under ``watch``; must never destroy prior state."""
    path = tmp_path / "state.db"
    first = rec.open_state(path)
    first.execute(
        "INSERT INTO event_delivery(event_id,camera,start_time,end_time,updated_at) VALUES('e','c',1,2,3)")
    first.commit()
    first.close()

    second = rec.open_state(path)
    try:
        assert second.execute("SELECT COUNT(*) FROM event_delivery").fetchone()[0] == 1
    finally:
        second.close()


def test_open_state_rows_are_addressable_by_name(tmp_path):
    """process_event indexes rows by column name; a plain tuple factory breaks it."""
    conn = rec.open_state(tmp_path / "state.db")
    try:
        conn.execute(
            "INSERT INTO event_delivery(event_id,camera,start_time,end_time,updated_at) VALUES('e','c',1,2,3)")
        row = conn.execute("SELECT * FROM event_delivery").fetchone()
        assert row["event_id"] == "e"
    finally:
        conn.close()


def test_event_delivery_primary_key_prevents_duplicates(state_db):
    state_db.execute(
        "INSERT INTO event_delivery(event_id,camera,start_time,end_time,updated_at) VALUES('e','c',1,2,3)")
    with pytest.raises(sqlite3.IntegrityError):
        state_db.execute(
            "INSERT INTO event_delivery(event_id,camera,start_time,end_time,updated_at) VALUES('e','c',1,2,4)")


def test_segment_delivery_is_keyed_per_event_and_path(state_db):
    """The same segment can overlap two events; each event tracks it separately."""
    for event_id in ("e1", "e2"):
        state_db.execute(
            "INSERT INTO segment_delivery(event_id,source_path,s3_key,etag,uploaded_at) VALUES(?,?,?,?,?)",
            (event_id, "/media/seg.mp4", "k", "tag", time.time()))
    assert state_db.execute("SELECT COUNT(*) FROM segment_delivery").fetchone()[0] == 2

    with pytest.raises(sqlite3.IntegrityError):
        state_db.execute(
            "INSERT INTO segment_delivery(event_id,source_path,s3_key,etag,uploaded_at) VALUES(?,?,?,?,?)",
            ("e1", "/media/seg.mp4", "k", "tag", time.time()))


def test_wal_mode_is_enabled(tmp_path):
    """`status` can be run while `watch` holds the DB open."""
    conn = rec.open_state(tmp_path / "state.db")
    try:
        assert conn.execute("PRAGMA journal_mode").fetchone()[0].lower() == "wal"
    finally:
        conn.close()


# --------------------------------------------------------------------------
# fetch_events
# --------------------------------------------------------------------------

@pytest.fixture
def source(tmp_path):
    def _build(events=(), recordings=()):
        db = build_source_db(tmp_path / f"f{len(list(tmp_path.iterdir()))}.db",
                             events=events, recordings=recordings)
        conn = sqlite3.connect(db)
        conn.row_factory = sqlite3.Row
        return conn
    return _build


def test_fetch_events_filters_by_label_and_camera(source, settings_factory):
    conn = source(events=[
        make_event(id="a", label="person", camera="door_camera"),
        make_event(id="b", label="car", camera="door_camera"),
        make_event(id="c", label="person", camera="garage"),
    ])
    got = rec.fetch_events(conn, "event", rec.table_columns(conn, "event"),
                           settings_factory(camera="door_camera", label="person"))
    assert [e["id"] for e in got] == ["a"]


def test_fetch_events_without_camera_filter_returns_every_camera(source, settings_factory):
    conn = source(events=[
        make_event(id="a", camera="door_camera"),
        make_event(id="c", camera="garage"),
    ])
    got = rec.fetch_events(conn, "event", rec.table_columns(conn, "event"),
                           settings_factory(camera=None))
    assert {e["id"] for e in got} == {"a", "c"}


def test_fetch_events_excludes_in_progress_events(source, settings_factory):
    """A NULL end_time means the person is still on camera. Archiving now would
    truncate the clip."""
    conn = source(events=[
        make_event(id="done", end_time=NOW + 20),
        make_event(id="live", end_time=None),
    ])
    got = rec.fetch_events(conn, "event", rec.table_columns(conn, "event"), settings_factory())
    assert [e["id"] for e in got] == ["done"]


def test_fetch_events_is_ordered_oldest_first(source, settings_factory):
    conn = source(events=[
        make_event(id="late", start_time=NOW + 100, end_time=NOW + 120),
        make_event(id="early", start_time=NOW, end_time=NOW + 20),
        make_event(id="mid", start_time=NOW + 50, end_time=NOW + 60),
    ])
    got = rec.fetch_events(conn, "event", rec.table_columns(conn, "event"), settings_factory())
    assert [e["id"] for e in got] == ["early", "mid", "late"]


def test_fetch_events_decodes_json_columns(source, settings_factory):
    conn = source(events=[make_event(zones='["porch","step"]', data='{"box":[1,2]}')])
    got = rec.fetch_events(conn, "event", rec.table_columns(conn, "event"), settings_factory())[0]
    assert got["zones"] == ["porch", "step"]
    assert got["data"] == {"box": [1, 2]}


def test_fetch_events_leaves_a_bare_name_sub_label_alone(source, settings_factory):
    conn = source(events=[make_event(sub_label="Jericho")])
    got = rec.fetch_events(conn, "event", rec.table_columns(conn, "event"), settings_factory())[0]
    assert got["sub_label"] == "Jericho"


def test_fetch_events_label_match_is_exact_and_case_sensitive(source, settings_factory):
    """.env.example promises exact matching; a mismatch returns 0 rows silently."""
    conn = source(events=[make_event(id="a", label="Person")])
    got = rec.fetch_events(conn, "event", rec.table_columns(conn, "event"),
                           settings_factory(label="person"))
    assert got == []


def test_fetch_events_only_selects_columns_that_exist(source, settings_factory):
    """An older schema without top_score/sub_label must not blow up the SELECT."""
    conn = source(events=[make_event()])
    narrow = {"id", "camera", "label", "start_time", "end_time"}
    got = rec.fetch_events(conn, "event", narrow, settings_factory())
    assert set(got[0]) == narrow


def test_fetch_events_hostile_camera_value_is_parameterised(source, settings_factory):
    """Filters are bound parameters, never interpolated."""
    conn = source(events=[make_event(id="a", camera="door_camera")])
    got = rec.fetch_events(conn, "event", rec.table_columns(conn, "event"),
                           settings_factory(camera="' OR 1=1 --"))
    assert got == []


# --------------------------------------------------------------------------
# fetch_segments
# --------------------------------------------------------------------------

def test_fetch_segments_returns_any_overlap(source):
    """Overlap, not containment — a segment that merely straddles the window edge
    still holds footage of the event."""
    conn = source(recordings=[
        make_recording(id="before", start_time=NOW - 500, end_time=NOW - 400),
        make_recording(id="straddles-start", start_time=NOW - 60, end_time=NOW + 5),
        make_recording(id="inside", start_time=NOW + 1, end_time=NOW + 10),
        make_recording(id="straddles-end", start_time=NOW + 30, end_time=NOW + 90),
        make_recording(id="after", start_time=NOW + 500, end_time=NOW + 600),
    ])
    got = rec.fetch_segments(conn, "recordings", rec.table_columns(conn, "recordings"),
                             "door_camera", NOW - 10, NOW + 35)
    assert [s["id"] for s in got] == ["straddles-start", "inside", "straddles-end"]


def test_fetch_segments_boundaries_are_inclusive(source):
    conn = source(recordings=[
        make_recording(id="touches-start", start_time=NOW - 60, end_time=NOW),
        make_recording(id="touches-end", start_time=NOW + 35, end_time=NOW + 90),
    ])
    got = rec.fetch_segments(conn, "recordings", rec.table_columns(conn, "recordings"),
                             "door_camera", NOW, NOW + 35)
    assert {s["id"] for s in got} == {"touches-start", "touches-end"}


def test_fetch_segments_is_scoped_to_one_camera(source):
    conn = source(recordings=[
        make_recording(id="ours", camera="door_camera"),
        make_recording(id="theirs", camera="garage"),
    ])
    got = rec.fetch_segments(conn, "recordings", rec.table_columns(conn, "recordings"),
                             "door_camera", NOW - 10, NOW + 35)
    assert [s["id"] for s in got] == ["ours"]


def test_fetch_segments_is_ordered_chronologically(source):
    conn = source(recordings=[
        make_recording(id="b", start_time=NOW + 10, end_time=NOW + 20),
        make_recording(id="a", start_time=NOW, end_time=NOW + 10),
    ])
    got = rec.fetch_segments(conn, "recordings", rec.table_columns(conn, "recordings"),
                             "door_camera", NOW - 100, NOW + 100)
    assert [s["id"] for s in got] == ["a", "b"], "clip order must be playback order"


def test_fetch_segments_empty_window_returns_nothing(source):
    conn = source(recordings=[make_recording()])
    got = rec.fetch_segments(conn, "recordings", rec.table_columns(conn, "recordings"),
                             "door_camera", NOW + 10_000, NOW + 20_000)
    assert got == []
