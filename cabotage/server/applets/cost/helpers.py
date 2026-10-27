"""Domain-agnostic helpers shared by the cost applet."""

import datetime

from cabotage.server.applets.cost.constants import GIB, MEM_SUFFIXES


def as_utc(dt: datetime.datetime) -> datetime.datetime:
    """``dt`` as an aware UTC datetime; naive values are taken to be UTC."""
    if dt.tzinfo is None:
        return dt.replace(tzinfo=datetime.UTC)
    return dt.astimezone(datetime.UTC)


def floor_hour(dt: datetime.datetime) -> datetime.datetime:
    return as_utc(dt).replace(minute=0, second=0, microsecond=0)


def parse_cpu_quantity(value: str | None) -> float | None:
    if value is None:
        return None
    if value.endswith("m"):
        return float(value[:-1]) / 1000.0
    return float(value)


def parse_memory_quantity_gib(value: str | None) -> float | None:
    if value is None:
        return None
    for suffix, multiplier in MEM_SUFFIXES.items():
        if value.endswith(suffix):
            return float(value[: -len(suffix)]) * multiplier / GIB
    return float(value) / GIB


def optional_float(value: str | None) -> float | None:
    return float(value) if value else None


def str_or_none(value: object) -> str | None:
    return value if isinstance(value, str) else None
