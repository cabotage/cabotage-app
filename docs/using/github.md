# Setting up auto-deploy from GitHub

Connect a GitHub repository so pushes to a branch automatically trigger image builds.

<ol class="guide-pipeline" role="list" markdown="block">
<li class="guide-step" markdown="block">
<div class="guide-step-number" aria-hidden="true">1</div>
<div class="guide-step-card" markdown="block">
<h3 class="guide-step-title" id="1-install-the-github-app">Install the GitHub App</h3>

Use the install button on your organization or application settings page to connect Cabotage to a GitHub account.

</div>
</li>
<li class="guide-step" markdown="block">
<div class="guide-step-number" aria-hidden="true">2</div>
<div class="guide-step-card" markdown="block">
<h3 class="guide-step-title" id="2-configure-your-application">Configure your application</h3>

Go to your application's **Settings** page and fill in:

| Field | Notes |
|---|---|
| `GitHub Repository` | e.g. `myorg/myapp` |
| `GitHub Installation` | choose the connected account |
| `Auto-deploy Branch` | e.g. `main`; surrounding whitespace is ignored |
| `Subdirectory` | optional — if your app isn't at the repo root |
| `GitHub Environment Name` | optional — for custom environment naming |

For projects with environments, **Settings > Application** keeps your selected environment in navigation, but its settings and Delete Application action apply across all environments. Saving or canceling returns to the selected environment. Use **Settings > Environment** for environment-specific settings or to remove the application from only that environment.

</div>
</li>
<li class="guide-step" markdown="block">
<div class="guide-step-number" aria-hidden="true">3</div>
<div class="guide-step-card" markdown="block">
<h3 class="guide-step-title" id="3-push-your-code">Push your code</h3>

Push to your configured branch. Once CI passes, Cabotage automatically builds a new image.

</div>
</li>
<li class="guide-step" markdown="block">
<div class="guide-step-number" aria-hidden="true">4</div>
<div class="guide-step-card" markdown="block">
<h3 class="guide-step-title" id="4-release-and-deploy">Release and deploy</h3>

You still create releases and deploy manually from the Overview tab. Fully automated deploy can be configured later.

</div>
</li>
</ol>

## Related

- [Deploying your code](deploying.md)
- [Configuration & environment variables](configuration.md)
