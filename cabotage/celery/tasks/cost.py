"""Periodic hourly cost collection task."""

import datetime
import logging

from celery import shared_task
from sqlalchemy.exc import SQLAlchemyError

from cabotage.server import db
from cabotage.server.models.cost import CostCollectionRun
from cabotage.utils.cost.collector import collect_hour

log = logging.getLogger(__name__)

#: How far back to backfill missing/failed hours on each invocation.
BACKFILL_HOURS = 48

#: How long to wait before retrying an hour whose most recent attempt failed.
FAILED_HOUR_RETRY_AFTER = datetime.timedelta(hours=6)


def _last_completed_hour(
    now: datetime.datetime | None = None,
) -> datetime.datetime:
    now = now or datetime.datetime.now(datetime.UTC)
    return now.replace(minute=0, second=0, microsecond=0) - datetime.timedelta(hours=1)


@shared_task()
def collect_costs() -> None:
    """Collect the last completed hour, plus any hour in the backfill window
    without a succeeded run, oldest first, capped at BACKFILL_HOURS hours.

    An hour that has never been attempted is always collected. An hour whose
    most recent attempt failed is retried only once FAILED_HOUR_RETRY_AFTER
    has passed since that attempt, so a sustained outage doesn't get
    hammered with the same failing query every beat tick.
    """
    last_hour = _last_completed_hour()
    window_start = last_hour - datetime.timedelta(hours=BACKFILL_HOURS - 1)
    window_hours = [
        window_start + datetime.timedelta(hours=i) for i in range(BACKFILL_HOURS)
    ]

    runs_by_hour: dict[datetime.datetime, CostCollectionRun] = {
        run.period_start: run
        for run in CostCollectionRun.query.filter(
            CostCollectionRun.period_start >= window_start,
            CostCollectionRun.period_start <= last_hour,
        ).all()
    }
    retry_before = last_hour - FAILED_HOUR_RETRY_AFTER

    def _should_collect(hour: datetime.datetime) -> bool:
        run = runs_by_hour.get(hour)
        if hour == last_hour or run is None:
            return True
        if run.status == "succeeded":
            return False
        last_attempt_at = (run.stats or {}).get("last_attempt_at")
        if not last_attempt_at:
            return True
        try:
            attempted_at = datetime.datetime.fromisoformat(last_attempt_at)
        except ValueError:
            return True
        return attempted_at <= retry_before

    to_collect = [hour for hour in window_hours if _should_collect(hour)]

    for hour in to_collect:
        try:
            run = collect_hour(hour)
        except SQLAlchemyError:
            db.session.rollback()
            log.exception("Cost collection failed for hour %s", hour.isoformat())
            continue
        stats = run.stats or {}
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
