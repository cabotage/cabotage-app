# Processes & Procfile

Your application's processes are defined in a `Procfile` at the root of your repository. Each line declares a process type and the command to run it.

## Process types

| Type | Behavior |
|---|---|
| `web` | The HTTP-facing process. Receives routed traffic from the load balancer. Must bind to a Unix socket provided by the platform. You can scale this to multiple pods. |
| `worker` | Background job processors, queue consumers, or any long-running process that doesn't serve HTTP. Scalable independently from web. |
| `release` | A one-off command that runs once during release creation. Use this for database migrations or other setup tasks that need to complete before traffic is routed. The release build will fail if this command exits non-zero. |
| `postdeploy` | A one-off command that runs once after each successful deploy. Useful for cache warming, notifications, or cleanup tasks. Failures here do not roll back the deploy. |

Only `web` and `worker` processes are scalable. `release` and `postdeploy` always run exactly once.

## Example Procfile

```
web: gunicorn app:app --bind unix:/tmp/nginx.socket
worker: celery -A app.celery worker --loglevel=info
release: flask db upgrade
```

## Related

- [Deploying your code](deploying.md) — how `release` and `postdeploy` commands run as part of the pipeline
- [Scheduled jobs](jobs.md) — the `job-` process prefix for cron-scheduled work
- [Pod sizes](pod-sizes.md)
