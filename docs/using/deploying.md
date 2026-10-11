# Deploying your code

Every deploy follows a four-step pipeline. You trigger each step from the application's Overview tab.

<ol class="guide-pipeline" role="list" markdown="block">
<li class="guide-step" markdown="block">
<div class="guide-step-number" aria-hidden="true">1</div>
<div class="guide-step-card" markdown="block">
<div class="guide-step-heading" markdown="block">
<svg aria-hidden="true" focusable="false" viewBox="0 0 24 24"><polygon points="5 3 19 12 5 21 5 3" /></svg>
<h3 class="guide-step-title">Build Image</h3>
</div>

Compiles your source code into a container image. This runs in a sandboxed build environment and produces a versioned, immutable image.

</div>
</li>
<li class="guide-step" markdown="block">
<div class="guide-step-number" aria-hidden="true">2</div>
<div class="guide-step-card" markdown="block">
<div class="guide-step-heading" markdown="block">
<svg aria-hidden="true" focusable="false" viewBox="0 0 24 24"><path d="M21 2l-2 2m-7.61 7.61a5.5 5.5 0 1 1-7.778 7.778 5.5 5.5 0 0 1 7.777-7.777zm0 0L15.5 7.5m0 0l3 3L22 7l-3-3m-3.5 3.5L19 4" /></svg>
<h3 class="guide-step-title">Configure</h3>
</div>

Configure environment variables that your application needs. These are versioned, allowing for rollback of both configuration and code. They can be marked as secret to ensure that they are not recoverable via the UI. See [Configuration & environment variables](configuration.md) for details.

</div>
</li>
<li class="guide-step" markdown="block">
<div class="guide-step-number" aria-hidden="true">3</div>
<div class="guide-step-card" markdown="block">
<div class="guide-step-heading" markdown="block">
<svg aria-hidden="true" focusable="false" viewBox="0 0 24 24"><circle cx="6" cy="6" r="3" /><circle cx="6" cy="18" r="3" /><line x1="20" y1="4" x2="8.12" y2="15.88" /><line x1="14.47" y1="14.48" x2="20" y2="20" /><line x1="8.12" y1="8.12" x2="12" y2="12" /></svg>
<h3 class="guide-step-title">Create Release</h3>
</div>

Snapshots the latest image together with your current environment variables into a single deployable unit. Nothing goes live yet — this just locks in what will be deployed.

</div>
</li>
<li class="guide-step" markdown="block">
<div class="guide-step-number" aria-hidden="true">4</div>
<div class="guide-step-card" markdown="block">
<div class="guide-step-heading" markdown="block">
<svg aria-hidden="true" focusable="false" viewBox="0 0 24 24"><polyline points="16 16 12 12 8 16" /><line x1="12" y1="12" x2="12" y2="21" /><path d="M20.39 18.39A5 5 0 0 0 18 9h-1.26A8 8 0 1 0 3 16.3" /></svg>
<h3 class="guide-step-title">Deploy</h3>
</div>

Pushes the release to your pods. Traffic switches over once the new pods pass health checks.

</div>
</li>
</ol>

## Recent Activity

Recent Activity links to your latest builds, releases, and deployments. Beside live status, the lists show additional entries when there is room, using up to 25 recent items per list. Stacked lists on narrow screens and pages without live status show up to three entries per list. Live pod status shows one row per process; hover over a shortened name to see it in full.

Log links in pull-request preview comments are marked with a lock because they require Cabotage access to the application.

## Config-only changes

If you only changed environment variables and your code hasn't changed, you can skip the build entirely:

```
Build (skipped) → Create Release → Deploy
```

The release automatically picks up the most recent built image. This is the fastest way to roll out a config change — no rebuild required.

## Release build commands

When you create a release, Cabotage runs any one-off commands defined in your Procfile before the release is finalized. These are shown as chips in the Processes section on the Overview tab.

How it works:

<ol class="guide-pipeline" role="list" markdown="block">
<li class="guide-step" markdown="block">
<div class="guide-step-number" aria-hidden="true">1</div>
<div class="guide-step-card" markdown="block">
<h3 class="guide-step-title">You click "Create Release"</h3>

Cabotage snapshots your latest image and current config into a new release.

</div>
</li>
<li class="guide-step" markdown="block">
<div class="guide-step-number" aria-hidden="true">2</div>
<div class="guide-step-card" markdown="block">
<h3 class="guide-step-title"><code>release</code> commands run</h3>

Each `release` process from your Procfile runs inside the new image. If any command exits non-zero, the release build fails and nothing is deployed.

</div>
</li>
<li class="guide-step" markdown="block">
<div class="guide-step-number" aria-hidden="true">3</div>
<div class="guide-step-card" markdown="block">
<h3 class="guide-step-title">Release is ready to deploy</h3>

Once all release commands succeed, the release is sealed and ready. After deploy, any `postdeploy` commands run automatically.

</div>
</li>
</ol>

Common patterns (choose the migration command for your framework; these are alternatives):

```text
release: flask db upgrade head
```

```text
release: python manage.py migrate
```

```text
postdeploy: python warm_cache.py
```

- `release` commands run during release build (DB migrations, etc.)
- `postdeploy` commands run after a successful deploy (cache warming, etc.)

These commands appear as chips below the process cards on the Overview tab. Hover over them to see the full command and what it does.

See [Processes & Procfile](processes.md) for how `release` and `postdeploy` process types are defined.

## Related

- [Processes & Procfile](processes.md)
- [Configuration & environment variables](configuration.md)
- [Auto-deploy from GitHub](github.md)
- [Pod sizes](pod-sizes.md)
- [Observing your application](observing.md)
