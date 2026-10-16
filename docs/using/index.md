# Using Cabotage

This section is for **application developers deploying their own applications on Cabotage** — not for people modifying Cabotage itself. If you're looking to work on Cabotage's own codebase, see [Contributing to Cabotage](../contributing/index.md). If you're administering a Cabotage platform deployment, see [Operating Cabotage](../operating/index.md).

## In this section

- [Deploying your code](deploying.md) — the build, configure, release, and deploy pipeline, plus the config-only shortcut
- [Configuration & environment variables](configuration.md) — secret vs. non-secret vs. build-time variables, the Raw Editor
- [Auto-deploy from GitHub](github.md) — connecting a repository to trigger automatic builds
- [Processes & Procfile](processes.md) — defining `web`, `worker`, `release`, and `postdeploy` processes
- [Scheduled jobs](jobs.md) — cron-scheduled Kubernetes CronJobs
- [Pod sizes](pod-sizes.md) — the full table of CPU and memory sizes
- [Observing your application](observing.md) — reading structured and raw logs
- [Access & organization roles](access.md) — Members vs. Admins and how project access works
