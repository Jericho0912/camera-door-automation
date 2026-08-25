"""Schema sniffing and the read-only snapshot.

The reconciler does not hard-code Frigate's schema; it discovers it. That makes
it resilient across Frigate versions and makes these the tests that protect
against a silent upgrade breaking ingestion.
"""
from __future__ import annotations

import sqlite3
import tempfile
from pathlib import Path

import pytest

import reconciler as rec
from conftest import build_source_db, make_event, make_recording


def _open(path: Path) -> sqlite3.Connection:
    return sqlite3.connect(path)


# --------------------------------------------------------------------------
# table_columns / resolve_table
# --------------------------------------------------------------------------

def test_table_columns_reads_the_real_schema(tmp_path):
    db = build_source_db(tmp_path / "f.db")
    conn = _open(db)
    try:
        assert rec.EVENT_REQUIRED <= rec.table_columns(conn, "event")
        assert rec.RECORDING_REQUIRED <= rec.table_columns(conn, "recordings")
    finally:
        conn.close()


def test_resolve_table_prefers_the_first_candidate(tmp_path):
    """Both 'event' and 'events' can exist; the ordered candidate list wins."""
    db = build_source_db(tmp_path / "f.db", extra_ddl=rec_events_alias())
    conn = _open(db)
    try:
        table, cols = rec.resolve_table(conn, ("event", "events"), rec.EVENT_REQUIRED)
        assert table == "event"
        assert rec.EVENT_REQUIRED <= cols
    finally:
        conn.close()


def rec_events_alias() -> str:
    from conftest import EVENT_DDL
    return EVENT_DDL.format(table='"events"') + ";"


def test_resolve_table_falls_back_to_a_unique_structural_match(tmp_path):
    """Frigate renamed the table; discovery still finds it by shape alone."""
    db = build_source_db(tmp_path / "f.db", event_table="tracked_object")
    conn = _open(db)
    try:
        table, _ = rec.resolve_table(conn, ("event", "events"), rec.EVENT_REQUIRED)
        assert table == "tracked_object"
    finally:
        conn.close()


def test_resolve_table_refuses_an_ambiguous_match(tmp_path):
    """Two same-shaped tables and neither is a known name: refuse rather than
    guess, because guessing wrong means archiving the wrong footage."""
    from conftest import EVENT_DDL
    db = build_source_db(
        tmp_path / "f.db",
        event_table="objects_a",
        extra_ddl=EVENT_DDL.format(table='"objects_b"') + ";",
    )
    conn = _open(db)
    try:
        with pytest.raises(RuntimeError, match="No unambiguous table"):
            rec.resolve_table(conn, ("event", "events"), rec.EVENT_REQUIRED)
    finally:
        conn.close()


def test_resolve_table_error_lists_what_it_did_find(tmp_path):
    """Operator-facing message quality: the error must be actionable."""
    conn = sqlite3.connect(tmp_path / "empty.db")
    conn.execute("CREATE TABLE unrelated (a TEXT)")
    try:
        with pytest.raises(RuntimeError) as err:
            rec.resolve_table(conn, ("event",), rec.EVENT_REQUIRED)
        msg = str(err.value)
        assert "unrelated" in msg and "start_time" in msg
    finally:
        conn.close()


def test_resolve_table_ignores_a_candidate_missing_required_columns(tmp_path):
    """A name match with the wrong shape must not be accepted."""
    from conftest import EVENT_DDL
    conn = sqlite3.connect(tmp_path / "f.db")
    conn.execute("CREATE TABLE event (id TEXT)")            # right name, wrong shape
    conn.executescript(EVENT_DDL.format(table='"events"') + ";")
    try:
        table, _ = rec.resolve_table(conn, ("event", "events"), rec.EVENT_REQUIRED)
        assert table == "events"
    finally:
        conn.close()


def test_resolve_table_survives_a_hostile_table_name(tmp_path):
    """qident must neutralise a table name containing a quote."""
    conn = sqlite3.connect(tmp_path / "f.db")
    from conftest import EVENT_DDL
    conn.executescript(EVENT_DDL.format(table='"ev""il"') + ";")
    try:
        table, cols = rec.resolve_table(conn, ("nope",), rec.EVENT_REQUIRED)
        assert table == 'ev"il'
        assert rec.EVENT_REQUIRED <= cols
    finally:
        conn.close()


# --------------------------------------------------------------------------
# snapshot_db
# --------------------------------------------------------------------------

def test_snapshot_is_an_independent_copy(tmp_path):
    """The whole point: later writes to the live DB must not be visible, so a
    pass sees one consistent view of events and recordings."""
    db = build_source_db(tmp_path / "f.db", events=[make_event()])
    snap = rec.snapshot_db(db)
    try:
        live = sqlite3.connect(db)
        live.execute("DELETE FROM event")
        live.commit()
        live.close()
        conn = sqlite3.connect(snap)
        assert conn.execute("SELECT COUNT(*) FROM event").fetchone()[0] == 1
        conn.close()
    finally:
        snap.unlink(missing_ok=True)


def test_snapshot_does_not_mutate_the_source(tmp_path):
    db = build_source_db(tmp_path / "f.db", events=[make_event()])
    before = db.read_bytes()
    snap = rec.snapshot_db(db)
    try:
        assert db.read_bytes() == before
    finally:
        snap.unlink(missing_ok=True)


def test_snapshot_missing_source_is_a_clear_error(tmp_path):
    with pytest.raises(FileNotFoundError, match="Fregata database not found"):
        rec.snapshot_db(tmp_path / "absent.db")


def test_snapshot_copies_recordings_too(tmp_path):
    db = build_source_db(tmp_path / "f.db",
                         events=[make_event()], recordings=[make_recording()])
    snap = rec.snapshot_db(db)
    try:
        conn = sqlite3.connect(snap)
        assert conn.execute("SELECT COUNT(*) FROM recordings").fetchone()[0] == 1
        conn.close()
    finally:
        snap.unlink(missing_ok=True)


def test_failed_snapshot_leaves_no_temp_file(tmp_path, monkeypatch):
    """A corrupt or locked source DB must not leak a snapshot — `watch` would
    recreate the leak every 30s until the disk fills."""
    spool = tmp_path / "spool"
    spool.mkdir()
    monkeypatch.setattr(tempfile, "tempdir", str(spool))

    corrupt = tmp_path / "corrupt.db"
    corrupt.write_bytes(b"this is definitely not a sqlite database" * 32)

    with pytest.raises(sqlite3.DatabaseError):
        rec.snapshot_db(corrupt)

    assert list(spool.iterdir()) == [], "temp snapshot leaked after a failed backup"
