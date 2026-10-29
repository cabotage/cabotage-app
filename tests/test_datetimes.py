"""Tests for ISO-8601 <-> naive UTC datetime conversions."""

import datetime

import pytest

from cabotage.utils.datetimes import iso_utc, parse_iso_utc


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        ("2026-10-11T14:27:00.000Z", datetime.datetime(2026, 10, 11, 14, 27)),
        ("2026-10-11T16:27:00+02:00", datetime.datetime(2026, 10, 11, 14, 27)),
        ("2026-10-11T23:30:00-05:00", datetime.datetime(2026, 10, 12, 4, 30)),
        ("2026-10-11T14:27:00", datetime.datetime(2026, 10, 11, 14, 27)),
    ],
)
def test_parse_iso_utc_converts_to_naive_utc(value, expected):
    # A naive datetime never equals an aware one, so this also checks naivety.
    assert parse_iso_utc(value) == expected


@pytest.mark.parametrize("value", [None, "", "yesterday", "2026-13-01T00:00:00Z"])
def test_parse_iso_utc_rejects_missing_or_invalid(value):
    assert parse_iso_utc(value) is None


def test_parse_iso_utc_round_trips_iso_utc():
    value = datetime.datetime(2026, 10, 11, 14, 27, 5, 123456)
    assert parse_iso_utc(iso_utc(value)) == value
