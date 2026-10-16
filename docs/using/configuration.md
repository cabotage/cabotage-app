# Configuration & environment variables

Add variables on the **Config** tab — one at a time, or use the **Raw Editor** to paste many at once in `KEY=VALUE` or JSON format. The Raw Editor creates new variables and updates existing ones; it never deletes variables you leave out. Values are saved exactly as typed, including quotes.

To add a single variable, paste `KEY=VALUE` into the **Name** field to fill both fields.

## Variable types

| Type | Behavior |
|---|---|
| **Secret** | Stored in Vault. Never displayed in the UI after creation. |
| **Non-secret** | Stored in Consul. Visible and editable on the Config tab. |
| **Build-time** | Available during image build. Useful for private registry tokens. |

## Changes take effect on release

Changes don't take effect until you create a new release. The release snapshots whatever config exists at that moment.

See [Deploying your code](deploying.md) for the full build/configure/release/deploy pipeline, including the config-only shortcut that skips the build step when only environment variables changed.

## Related

- [Deploying your code](deploying.md)
- [Auto-deploy from GitHub](github.md)
