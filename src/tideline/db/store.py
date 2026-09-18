"""The database file in S3.

Lambda has no disk that survives between invocations, so the SQLite file lives
in S3 and each function works on a copy in /tmp:

- the run downloads it, writes its results, and uploads it back;
- the dashboard downloads it when it has changed (it asks S3 for the file's
  ETag, a fingerprint of its contents, on each request) and reads the copy.

Uploads are conditional: "only replace the file if it is still the version I
downloaded" (S3's If-Match). If the dashboard accepted a DNS change while a run
was going, the second upload is refused instead of silently erasing the first,
and the error says so. At two runs a month that is unlikely, but losing data
quietly is never acceptable.

S3 versioning is on for the bucket (infra/s3.tf), so every upload is also a
backup: an earlier version of the file can be restored from the S3 console.
"""

import logging
import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import boto3
from botocore.exceptions import ClientError

from tideline.observability.logging import log_event

log = logging.getLogger("tideline.store")


class StaleCopy(Exception):
    """The file in S3 changed since this copy was downloaded; the upload was refused."""


@dataclass
class S3Database:
    bucket: str
    key: str
    path: Path
    region: str = "ca-central-1"
    etag: str | None = None
    _client: Any = field(default=None, repr=False)

    @property
    def client(self) -> Any:
        if self._client is None:
            self._client = boto3.client("s3", region_name=self.region)
        return self._client

    def download(self) -> bool:
        """Fetch the file if it changed since the last download. Returns True if it did.

        A missing file is fine: the first run starts from an empty database.
        """
        kwargs: dict[str, Any] = {"Bucket": self.bucket, "Key": self.key}
        if self.etag and self.path.exists():
            kwargs["IfNoneMatch"] = self.etag
        try:
            response = self.client.get_object(**kwargs)
        except ClientError as exc:
            code = exc.response.get("Error", {}).get("Code")
            status = exc.response.get("ResponseMetadata", {}).get("HTTPStatusCode")
            if status == 304 or code == "304":
                return False  # unchanged
            if code in ("NoSuchKey", "404"):
                self.etag = None
                log_event(log, "database_not_in_s3_yet", key=self.key)
                return False
            raise
        # Write beside the target, then rename: a reader never sees half a file.
        partial = self.path.with_suffix(".download")
        with partial.open("wb") as out:
            for chunk in response["Body"].iter_chunks(1 << 20):
                out.write(chunk)
        os.replace(partial, self.path)
        self.etag = response["ETag"]
        log_event(log, "database_downloaded", bytes=self.path.stat().st_size)
        return True

    def upload(self) -> None:
        """Replace the file in S3 with this copy, if nobody else replaced it first."""
        kwargs: dict[str, Any] = {"Bucket": self.bucket, "Key": self.key}
        if self.etag:
            kwargs["IfMatch"] = self.etag
        else:
            kwargs["IfNoneMatch"] = "*"  # first upload: only if there is still nothing there
        try:
            with self.path.open("rb") as body:
                response = self.client.put_object(Body=body, **kwargs)
        except ClientError as exc:
            if exc.response.get("Error", {}).get("Code") in ("PreconditionFailed", "412"):
                raise StaleCopy(f"s3://{self.bucket}/{self.key} changed during this run") from exc
            raise
        self.etag = response["ETag"]
        log_event(log, "database_uploaded", bytes=self.path.stat().st_size)
