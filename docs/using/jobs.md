# Scheduled jobs

Jobs are cron-scheduled tasks that run as Kubernetes CronJobs. Define them in your Procfile with a `job-` prefix and set the `SCHEDULE` env var inline with a cron expression.

## How jobs work

| | |
|---|---|
| `job-*` | Any process in your Procfile with a `job-` prefix is treated as a scheduled job. Each job runs independently as a Kubernetes CronJob on its own schedule. |
| `SCHEDULE` | Each job process requires a `SCHEDULE` env var set to a standard cron expression, defined inline in the Procfile. |

## Behavior

| | |
|---|---|
| **Concurrency** | Overlapping runs are forbidden. If a job is still running when the next scheduled invocation arrives, the new run is skipped. |
| **Timeout** | Each run has a 1-hour active deadline. Jobs that exceed this are terminated. |
| **Retries** | Jobs do not retry on failure. A failed run is recorded and the next invocation runs on the normal schedule. |
| **Controls** | On the Overview tab you can suspend/resume jobs with the toggle and change pod size from the dropdown. Both take effect immediately. |

## Example

```
job-cleanup: env SCHEDULE="0 */6 * * *" python manage.py cleanup_stale_sessions
job-reports: env SCHEDULE="0 2 * * *" python manage.py generate_daily_report
```

The `SCHEDULE` env var is set inline in the Procfile using `env SCHEDULE="<cron expression>"` before the command. Each job gets its own independent schedule.

## Related

- [Processes & Procfile](processes.md)
- [Pod sizes](pod-sizes.md) — jobs can be resized from the Overview tab
