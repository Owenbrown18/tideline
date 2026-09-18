"""The database file in S3 (tideline.db.store), against moto's fake S3."""

import boto3
import pytest
from moto import mock_aws

from tideline.db.store import S3Database, StaleCopy

BUCKET = "tideline-test"
REGION = "ca-central-1"


@pytest.fixture
def aws(monkeypatch):
    for name in ("AWS_ACCESS_KEY_ID", "AWS_SECRET_ACCESS_KEY", "AWS_SESSION_TOKEN"):
        monkeypatch.setenv(name, "testing")
    monkeypatch.delenv("AWS_PROFILE", raising=False)
    with mock_aws():
        boto3.client("s3", region_name=REGION).create_bucket(
            Bucket=BUCKET, CreateBucketConfiguration={"LocationConstraint": REGION}
        )
        yield


def store(tmp_path, name="a.db") -> S3Database:
    return S3Database(BUCKET, "tideline.db", tmp_path / name, REGION)


def test_first_run_starts_empty_then_uploads(aws, tmp_path):
    db = store(tmp_path)
    assert db.download() is False  # nothing in S3 yet
    db.path.write_bytes(b"first")
    db.upload()
    other = store(tmp_path, "b.db")
    assert other.download() is True
    assert other.path.read_bytes() == b"first"


def test_download_only_transfers_when_the_file_changed(aws, tmp_path):
    writer = store(tmp_path, "w.db")
    writer.path.write_bytes(b"v1")
    writer.upload()

    reader = store(tmp_path, "r.db")
    assert reader.download() is True
    assert reader.download() is False  # unchanged: nothing transferred

    writer.path.write_bytes(b"v2")
    writer.upload()
    assert reader.download() is True
    assert reader.path.read_bytes() == b"v2"


def test_an_upload_over_a_newer_file_is_refused(aws, tmp_path):
    seed = store(tmp_path, "seed.db")
    seed.path.write_bytes(b"v1")
    seed.upload()

    run = store(tmp_path, "run.db")
    run.download()
    dashboard = store(tmp_path, "dash.db")
    dashboard.download()

    dashboard.path.write_bytes(b"dns accepted")
    dashboard.upload()

    run.path.write_bytes(b"run results")
    with pytest.raises(StaleCopy):
        run.upload()  # would have erased the accepted DNS change

    check = store(tmp_path, "check.db")
    check.download()
    assert check.path.read_bytes() == b"dns accepted"


def test_first_upload_does_not_overwrite_an_existing_file(aws, tmp_path):
    existing = store(tmp_path, "e.db")
    existing.path.write_bytes(b"real data")
    existing.upload()

    fresh = store(tmp_path, "f.db")  # never downloaded, so it thinks S3 is empty
    fresh.path.write_bytes(b"empty")
    with pytest.raises(StaleCopy):
        fresh.upload()
