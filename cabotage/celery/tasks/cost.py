"""Periodic hourly cost collection task."""

import datetime
import logging

from celery import shared_task

from cabotage.server.models.cost import CostCollectionRun
from cabotage.utils.cost.collector import collect_hour

log = logging.getLogger(__name__)

#: How far back to backfill missing/failed hours on each invocation.
BACKFILL_HOURS = 48


def _last_completed_hour(
    now: datetime.datetime | None = None,
) -> datetime.datetime:
    now = now or datetime.datetime.now(datetime.timezone.utc)
    return now.replace(minute=0, second=0, microsecond=0) - datetime.timedelta(hours=1)


@shared_task()
def collect_costs() -> None:
    """Collect the last completed hour, plus any hour in the backfill window
    without a succeeded run, oldest first, capped at BACKFILL_HOURS hours.
    """
    last_hour = _last_completed_hour()
    window_start = last_hour - datetime.timedelta(hours=BACKFILL_HOURS - 1)
    window_hours = [
        window_start + datetime.timedelta(hours=i) for i in range(BACKFILL_HOURS)
    ]

    succeeded: set[datetime.datetime] = {
        run.period_start
        for run in CostCollectionRun.query.filter(
            CostCollectionRun.period_start >= window_start,
            CostCollectionRun.period_start <= last_hour,
            CostCollectionRun.status == "succeeded",
        ).all()
    }

    to_collect = [
        hour for hour in window_hours if hour == last_hour or hour not in succeeded
    ][:BACKFILL_HOURS]

    for hour in to_collect:
        try:
            run = collect_hour(hour)
        except Exception:
            log.exception("Cost collection failed for hour %s", hour.isoformat())
            continue
        stats: dict[str, object] = run.stats or {}
        if stats.get("last_attempt_status") == "failed":
            suffix = (
                " (kept previous succeeded data)" if run.status == "succeeded" else ""
            )
            log.warning(
                "Cost collection attempt failed for hour %s: %s%s",
                hour.isoformat(),
                stats.get("last_attempt_error") or run.error,
                suffix,
            )
