"""The UTC timestamp column type (tideline.db.models.UTCDateTime)."""

from datetime import UTC, datetime, timedelta, timezone

import pytest

from tideline.db.models import UTCDateTime

TYPE = UTCDateTime()


def test_times_are_stored_as_utc_and_come_back_aware():
    pacific = timezone(timedelta(hours=-7))
    stored = TYPE.process_bind_param(datetime(2026, 9, 17, 5, 0, tzinfo=pacific), None)  # type: ignore[arg-type]
    assert stored == datetime(2026, 9, 17, 12, 0)  # naive UTC in the file
    read = TYPE.process_result_value(stored, None)  # type: ignore[arg-type]
    assert read == datetime(2026, 9, 17, 12, 0, tzinfo=UTC)
    assert read.tzinfo is UTC


def test_a_naive_time_is_refused():
    with pytest.raises(ValueError, match="naive"):
        TYPE.process_bind_param(datetime(2026, 9, 17, 12, 0), None)  # type: ignore[arg-type]


def test_none_passes_through():
    assert TYPE.process_bind_param(None, None) is None  # type: ignore[arg-type]
    assert TYPE.process_result_value(None, None) is None  # type: ignore[arg-type]
