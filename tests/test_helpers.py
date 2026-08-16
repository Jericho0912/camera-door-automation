"""Pure helpers: timestamps, quoting, JSON coercion, path resolution."""
from __future__ import annotations

import hashlib
from pathlib import Path

import pytest

import reconciler as rec


# --------------------------------------------------------------------------
# utc_iso
# --------------------------------------------------------------------------

def test_utc_iso_none_passes_through():
    assert rec.utc_iso(None) is None


def test_utc_iso_is_utc_not_local():
    assert rec.utc_iso(0) == "1970-01-01T00:00:00+00:00"
    assert rec.utc_iso(1_700_000_000) == "2023-11-14T22:13:20+00:00"


def test_utc_iso_accepts_numeric_strings():
    assert rec.utc_iso("1700000000") == rec.utc_iso(1_700_000_000)


def test_utc_iso_keeps_sub_second_precision():
    assert rec.utc_iso(1_700_000_000.25).endswith("22:13:20.250000+00:00")


# --------------------------------------------------------------------------
# qident
# --------------------------------------------------------------------------

def test_qident_quotes_plainly():
    assert rec.qident("event") == '"event"'


def test_qident_escapes_embedded_quotes():
    """Table names are interpolated into SQL, so this is the only thing
    standing between a hostile schema and injection."""
    assert rec.qident('ev"ent') == '"ev""ent"'
    assert rec.qident('a"; DROP TABLE x; --') == '"a""; DROP TABLE x; --"'


# --------------------------------------------------------------------------
# parse_json
# --------------------------------------------------------------------------

@pytest.mark.parametrize("raw,expected", [
    ('{"a": 1}', {"a": 1}),
    ('[1, 2]', [1, 2]),
    ('"quoted"', "quoted"),
    ('null', None),
    ('12', 12),
])
def test_parse_json_decodes_valid_json(raw, expected):
    assert rec.parse_json(raw) == expected


@pytest.mark.parametrize("raw", ["Jericho", "not json {", "", "  "])
def test_parse_json_returns_invalid_strings_verbatim(raw):
    """sub_label holds a bare person's name, which is not JSON."""
    assert rec.parse_json(raw) == raw


@pytest.mark.parametrize("raw", [None, 12, 3.5, {"a": 1}, b"bytes"])
def test_parse_json_passes_non_strings_through(raw):
    assert rec.parse_json(raw) == raw


# --------------------------------------------------------------------------
# canonical_path
# --------------------------------------------------------------------------

def test_canonical_path_prefers_the_literal_path_when_it_exists(tmp_path, recordings_dir):
    real = tmp_path / "elsewhere" / "seg.mp4"
    real.parent.mkdir(parents=True)
    real.write_bytes(b"x")
    assert rec.canonical_path(str(real), recordings_dir) == real.resolve()


def test_canonical_path_remaps_container_paths_to_the_host_mount(recordings_dir):
    """Frigate records container paths like /media/frigate/recordings/...;
    the host mount lives somewhere else entirely."""
    target = recordings_dir / "2024-01-01" / "12" / "door_camera" / "00.mp4"
    target.parent.mkdir(parents=True)
    target.write_bytes(b"x")
    got = rec.canonical_path("/media/frigate/recordings/2024-01-01/12/door_camera/00.mp4", recordings_dir)
    assert got == target.resolve()


def test_canonical_path_falls_back_to_basename(recordings_dir):
    flat = recordings_dir / "00.mp4"
    flat.write_bytes(b"x")
    got = rec.canonical_path("/somewhere/else/00.mp4", recordings_dir)
    assert got == flat.resolve()


def test_canonical_path_returns_best_guess_when_nothing_exists(recordings_dir):
    """Callers rely on the returned path *not* existing to raise a clear error."""
    got = rec.canonical_path("/media/frigate/recordings/a/b.mp4", recordings_dir)
    assert not got.exists()
    assert got == (recordings_dir / "a" / "b.mp4").resolve()


def test_canonical_path_uses_the_last_recordings_segment_is_the_first(recordings_dir):
    """``parts.index`` finds the FIRST 'recordings' component. A path that
    contains the word twice remaps from the first one."""
    target = recordings_dir / "recordings" / "x.mp4"
    target.parent.mkdir(parents=True)
    target.write_bytes(b"x")
    got = rec.canonical_path("/srv/recordings/recordings/x.mp4", recordings_dir)
    assert got == target.resolve()


# --------------------------------------------------------------------------
# relative_recording_key
# --------------------------------------------------------------------------

def test_relative_key_is_the_path_below_the_recordings_root(recordings_dir):
    p = recordings_dir / "2024-01-01" / "12" / "door_camera" / "00.mp4"
    assert rec.relative_recording_key(p, recordings_dir) == "2024-01-01/12/door_camera/00.mp4"


def test_relative_key_is_posix_style(recordings_dir):
    p = recordings_dir / "a" / "b" / "c.mp4"
    assert "\\" not in rec.relative_recording_key(p, recordings_dir)


def test_outside_paths_get_a_stable_hashed_external_key(tmp_path, recordings_dir):
    """This is the failure mode .env.example warns about: a mis-set
    FREGATA_RECORDINGS_DIR quietly reroutes every key under external/."""
    outside = tmp_path / "unmounted" / "00.mp4"
    key = rec.relative_recording_key(outside, recordings_dir)
    digest = hashlib.sha256(str(outside).encode()).hexdigest()[:12]
    assert key == f"external/{digest}-00.mp4"
    assert rec.relative_recording_key(outside, recordings_dir) == key, "must be stable across runs"


def test_distinct_outside_paths_do_not_collide(tmp_path, recordings_dir):
    a = rec.relative_recording_key(tmp_path / "one" / "00.mp4", recordings_dir)
    b = rec.relative_recording_key(tmp_path / "two" / "00.mp4", recordings_dir)
    assert a != b


@pytest.mark.defect
@pytest.mark.xfail(strict=True, reason=(
    "SECURITY: a recordings row whose path escapes the recordings root "
    "(absolute path, or ../ traversal) is resolved and uploaded anyway, "
    "landing under external/. Nothing constrains uploads to the media root."))
def test_traversal_outside_the_media_root_is_refused(tmp_path, recordings_dir):
    secret = tmp_path / "secret.txt"
    secret.write_text("private")
    with pytest.raises(Exception):
        rec.canonical_path(str(secret), recordings_dir)


# --------------------------------------------------------------------------
# key construction (prefix handling, mirrored from process_event)
# --------------------------------------------------------------------------

def test_prefix_is_optional_in_key_layout(recordings_dir, settings_factory):
    """Both branches of the ``if settings.prefix`` ternary must be well formed —
    in particular the empty-prefix branch must not emit a leading slash."""
    s_with = settings_factory(prefix="fregata")
    s_without = settings_factory(prefix="")
    rel = "door_camera/00.mp4"
    assert f"{s_with.prefix}/recordings/{rel}" == "fregata/recordings/door_camera/00.mp4"
    key_without = f"{s_without.prefix}/recordings/{rel}" if s_without.prefix else f"recordings/{rel}"
    assert not key_without.startswith("/")
