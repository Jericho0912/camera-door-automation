"""S3 upload behaviour, exercised against moto rather than a hand-rolled stub."""
from __future__ import annotations

import json

import pytest
from boto3.exceptions import S3UploadFailedError

import reconciler as rec


# --------------------------------------------------------------------------
# client construction
# --------------------------------------------------------------------------

def test_client_honours_a_custom_endpoint(aws_credentials, settings_factory):
    """The R2 path in .env.example depends on this."""
    c = rec.s3_client(settings_factory(endpoint_url="https://acct.r2.cloudflarestorage.com",
                                       region="auto"))
    assert c.meta.endpoint_url == "https://acct.r2.cloudflarestorage.com"


def test_client_retries_are_configured(aws_credentials, settings_factory):
    """A flaky home uplink is the expected operating condition.

    botocore normalises ``max_attempts=8`` (retries) into
    ``total_max_attempts=9`` (the initial call plus 8 retries).
    """
    c = rec.s3_client(settings_factory())
    assert c.meta.config.retries["total_max_attempts"] == 9
    assert c.meta.config.retries["mode"] == "standard"


# --------------------------------------------------------------------------
# upload_file
# --------------------------------------------------------------------------

def test_upload_file_puts_the_bytes_and_returns_an_etag(s3, settings_factory, segment_file):
    src = segment_file()
    settings = settings_factory()
    etag = rec.upload_file(s3, settings, src, "fregata/recordings/door_camera/seg-1.mp4")

    body = s3.get_object(Bucket="test-bucket", Key="fregata/recordings/door_camera/seg-1.mp4")["Body"].read()
    assert body == src.read_bytes()
    assert etag and '"' not in etag, "quotes must be stripped from the ETag"


def test_upload_file_sets_a_video_content_type(s3, settings_factory, segment_file):
    src = segment_file("door_camera/seg.mp4")
    rec.upload_file(s3, settings_factory(), src, "k.mp4")
    assert s3.head_object(Bucket="test-bucket", Key="k.mp4")["ContentType"] == "video/mp4"


def test_upload_file_falls_back_to_octet_stream(s3, settings_factory, segment_file):
    src = segment_file("door_camera/seg.unknownext")
    rec.upload_file(s3, settings_factory(), src, "k.unknownext")
    assert s3.head_object(Bucket="test-bucket", Key="k.unknownext")["ContentType"] == "application/octet-stream"


def test_dry_run_uploads_nothing(s3, settings_factory, segment_file, caplog):
    src = segment_file()
    with caplog.at_level("INFO"):
        etag = rec.upload_file(s3, settings_factory(dry_run=True), src, "k.mp4")
    assert etag is None
    assert s3.list_objects_v2(Bucket="test-bucket").get("Contents") is None
    assert "DRY RUN upload" in caplog.text


def test_dry_run_does_not_require_a_bucket(s3, settings_factory, segment_file):
    """Dry run must work on a fresh install before credentials exist."""
    assert rec.upload_file(s3, settings_factory(dry_run=True, bucket=""), segment_file(), "k") is None


def test_live_mode_without_a_bucket_fails_loudly(s3, settings_factory, segment_file):
    with pytest.raises(RuntimeError, match="S3_BUCKET is required"):
        rec.upload_file(s3, settings_factory(dry_run=False, bucket=""), segment_file(), "k")


def test_upload_to_a_missing_bucket_surfaces_the_aws_error(s3, settings_factory, segment_file):
    """boto3's managed transfer re-wraps ClientError as S3UploadFailedError, so
    the exception the caller must expect is *not* the botocore one. run_once
    catches bare Exception, so either way the event is recorded as failed."""
    with pytest.raises(S3UploadFailedError, match="NoSuchBucket"):
        rec.upload_file(s3, settings_factory(bucket="no-such-bucket"), segment_file(), "k")


def test_upload_overwrites_the_same_key(s3, settings_factory, segment_file):
    """Re-delivery must be idempotent at the object level, not append."""
    settings = settings_factory()
    rec.upload_file(s3, settings, segment_file("a.mp4", b"first"), "k.mp4")
    rec.upload_file(s3, settings, segment_file("b.mp4", b"second"), "k.mp4")
    assert s3.get_object(Bucket="test-bucket", Key="k.mp4")["Body"].read() == b"second"
    assert s3.list_objects_v2(Bucket="test-bucket")["KeyCount"] == 1


# --------------------------------------------------------------------------
# upload_manifest
# --------------------------------------------------------------------------

def test_manifest_is_stored_as_json(s3, settings_factory):
    manifest = {"schema_version": 1, "segments": [{"s3_key": "a"}]}
    rec.upload_manifest(s3, settings_factory(), "fregata/events/door/e1/manifest.json", manifest)

    obj = s3.get_object(Bucket="test-bucket", Key="fregata/events/door/e1/manifest.json")
    assert obj["ContentType"] == "application/json"
    assert json.loads(obj["Body"].read()) == manifest


def test_manifest_keys_are_sorted_for_stable_diffs(s3, settings_factory):
    rec.upload_manifest(s3, settings_factory(), "m.json", {"z": 1, "a": 2, "m": 3})
    body = s3.get_object(Bucket="test-bucket", Key="m.json")["Body"].read().decode()
    assert body.index('"a"') < body.index('"m"') < body.index('"z"')


def test_manifest_preserves_non_ascii(s3, settings_factory):
    rec.upload_manifest(s3, settings_factory(), "m.json", {"person": "José Ángel"})
    body = s3.get_object(Bucket="test-bucket", Key="m.json")["Body"].read().decode("utf-8")
    assert "José Ángel" in body, "ensure_ascii=False must survive the round trip"


def test_dry_run_manifest_is_logged_not_uploaded(s3, settings_factory, caplog):
    with caplog.at_level("INFO"):
        rec.upload_manifest(s3, settings_factory(dry_run=True), "m.json", {"a": 1})
    assert s3.list_objects_v2(Bucket="test-bucket").get("Contents") is None
    assert "DRY RUN upload manifest" in caplog.text


@pytest.mark.defect
@pytest.mark.xfail(strict=True, reason=(
    "PRIVACY: the dry-run branch logs the entire manifest at INFO, including the "
    "embedded event row and its sub_label — a real person's name — to a log file "
    "that .env.example is otherwise careful to keep names out of."))
def test_dry_run_manifest_does_not_log_personal_names(s3, settings_factory, caplog):
    manifest = {"event": {"id": "e1", "sub_label": "Jericho"}}
    with caplog.at_level("INFO"):
        rec.upload_manifest(s3, settings_factory(dry_run=True), "m.json", manifest)
    assert "Jericho" not in caplog.text
