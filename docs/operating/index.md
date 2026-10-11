# Operating Cabotage

This section is for **platform administrators running Cabotage itself**. It covers platform-wide settings and cluster integration, not ordinary application deployment or organization membership management.

If you deploy applications on an existing platform, read [Using Cabotage](../using/index.md). If you are changing Cabotage's code or tooling, read [Contributing to Cabotage](../contributing/index.md).

## Platform guides

- [Authentication and multi-factor authentication](authentication.md) — password and GitHub sign-in, passkeys, TOTP, recovery codes, and startup validation.
- [Rolling out MFA](rollout-mfa.md) — production prerequisites, deployment order, gradual enforcement, verification, and rollback.
- [Network policies](network-policies.md) — tenant namespace isolation, allowed ingress and egress, and rollout checks.
- [Tailscale ingress](tailscale.md) — cluster prerequisites and per-organization tailnet integration through workload identity federation.

These guides describe settings for the Cabotage service and its infrastructure. They are separate from the environment variables configured for a deployed application on its Config tab.
