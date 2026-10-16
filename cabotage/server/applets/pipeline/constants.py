import datetime

PIPELINE_RANGES = {"7d": 7, "30d": 30, "90d": 90}
# Above this many app envs with zero runs in range, the infra rollup hides
# them behind a disclosure instead of shipping every inventory row.
INFRA_PIPELINE_INACTIVE_LIMIT = 300
# A run whose last stage succeeded without the next one starting this long
# after is over (e.g. an image built for an app that doesn't auto-deploy).
PIPELINE_HANDOFF = datetime.timedelta(minutes=2)
# Build and deploy jobs are killed at their activeDeadlineSeconds (an hour at
# most), so a stage unfinished past this has stalled rather than still running.
PIPELINE_STALE = datetime.timedelta(hours=1)
