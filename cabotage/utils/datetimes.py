"""Conversions between naive UTC datetimes and ISO-8601 strings.

The models store naive UTC datetimes; browsers and JSON payloads exchange
ISO-8601 strings with an explicit ``Z`` so JavaScript parses them as UTC.
"""

import datetime


def iso_utc(value: datetime.datetime | None) -> str | None:
    """Format a naive UTC datetime as ISO-8601 with a ``Z`` suffix."""
    return value.isoformat() + "Z" if value else None


def parse_iso_utc(value: str | None) -> datetime.datetime | None:
    """Parse an ISO-8601 timestamp into a naive UTC datetime.

    Accepts a ``Z`` suffix or any UTC offset (converted to UTC); a timestamp
    without an offset is taken to already be UTC. Returns None when the value
    is missing or not a valid timestamp.
    """
    if not value:
        return None
    try:
        parsed = datetime.datetime.fromisoformat(value)
    except ValueError:
        return None
    if parsed.tzinfo is not None:
        parsed = parsed.astimezone(datetime.UTC).replace(tzinfo=None)
    return parsed
