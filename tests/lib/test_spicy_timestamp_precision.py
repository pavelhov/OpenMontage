from datetime import datetime, timezone

import pytest

from lib.spicy_catalog import parse_utc


@pytest.mark.parametrize("stamp, expected_microsecond", [
    ("2026-10-23T04:32:11Z", 0),
    ("2026-10-23T04:32:11.2Z", 200000),
    ("2026-10-23T04:32:11.273526Z", 273526),
    ("2026-10-23T04:32:11.2735269Z", 273526),
    ("2026-10-23T04:32:11.273526903Z", 273526),
    ("2026-10-23T04:32:11.273526903+00:00", 273526),
])
def test_parse_utc_accepts_rfc3339_fractional_seconds_up_to_nanoseconds(stamp, expected_microsecond):
    parsed = parse_utc(stamp)

    assert parsed == datetime(2026, 10, 23, 4, 32, 11, expected_microsecond, tzinfo=timezone.utc)


@pytest.mark.parametrize("stamp", [
    "2026-10-23T04:32:11.1234567890Z",
    "2026-10-23T04:32:11.123456789+01:00",
    "2026-10-23T04:32:11.123456789-00:00",
    "2026-10-23 04:32:11.123456789Z",
    "2026-10-23T04:32:11.123456789z",
    "٢٠٢٦-10-23T04:32:11.123456789Z",
    "2026-02-30T04:32:11.123456789Z",
])
def test_parse_utc_rejects_invalid_precision_or_non_utc_stamps(stamp):
    assert parse_utc(stamp) is None


@pytest.mark.parametrize("value", [None, True, 1, {}, []])
def test_parse_utc_rejects_non_string_values(value):
    assert parse_utc(value) is None
