# Operating Cabotage

This section is for **platform administrators running Cabotage itself**. It covers platform-wide settings and cluster integration, not ordinary application deployment or organization membership management.

If you deploy applications on an existing platform, read [Using Cabotage](../using/index.md). If you are changing Cabotage's code or tooling, read [Contributing to Cabotage](../contributing/index.md).

## Platform guides

- [Authentication and multi-factor authentication](authentication.md) — password and GitHub sign-in, passkeys, TOTP, recovery codes, and startup validation.
- [Rolling out MFA](rollout-mfa.md) — production prerequisites, deployment order, gradual enforcement, verification, and rollback.
- [Network policies](network-policies.md) — tenant namespace isolation, allowed ingress and egress, and rollout checks.
- [Tailscale ingress](tailscale.md) — cluster prerequisites and per-organization tailnet integration through workload identity federation.

These guides describe settings for the Cabotage service and its infrastructure. They are separate from the environment variables configured for a deployed application on its Config tab.

## Documentation links

All Cabotage instances (Cabo Prime, PSF Cabo, PyPI Cabo) share one documentation site, built from this repository and published at `https://cabotage.github.io/cabotage-app/`. The app's "Docs" navigation links and in-app help links use this URL by default via the `CABOTAGE_DOCS_URL` setting. No documentation is built or hosted inside the app itself.

Operators may optionally override `CABOTAGE_DOCS_URL` to point at a different build of the same docs (for example, a fork or a staging deployment). This is not required for normal operation.
