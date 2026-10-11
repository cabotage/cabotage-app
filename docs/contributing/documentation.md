# Contributing to the documentation

This page is for people working **on Cabotage itself** — adding or changing
its documentation. For developing and deploying your own applications with
Cabotage, see [Using Cabotage](../using/index.md).

## How the site is organized

This documentation is built with [Zensical](https://zensical.org/) from
Markdown files in `docs/`, configured by the root `zensical.toml`. Pages are
grouped by audience:

- **Using Cabotage** (`docs/using/`) — for application developers deploying
  on top of a Cabotage platform.
- **Operating Cabotage** (the operator guides at the root of `docs/`, such as
  `authentication.md`, `tailscale.md`, `network-policies.md`, and
  `rollout-mfa.md`) — for platform administrators running Cabotage.
- **Contributing to Cabotage** (`docs/contributing/`, this section) — for
  people changing Cabotage's own code, tooling, or documentation.

When you add a page, place it in the section matching its audience and link
it from the navigation in root `zensical.toml`.

The audience sections and their pages appear in a collapsible left sidebar
rather than top-level tabs. Use each section's chevron to show or hide its pages;
Using Cabotage starts expanded on every page, including Home, through
`docs/javascripts/navigation.js`; the current section also starts expanded.
Other sections stay collapsed until opened. On smaller screens, open the
navigation menu to use the same page hierarchy.

The header uses `docs/overrides/.icons/cabotage.svg`, copied from the application's
`cabotage/client/static/cabotage.svg`. It is rendered inline with `currentColor`
so the logo follows the light and dark themes. Keep the two assets aligned
when updating the Cabotage logo.

The deployment, release, and GitHub setup timelines preserve the app Guide's
numbered steps. Their shared styles live in `docs/stylesheets/guide.css` and
use Zensical's theme colors. Keep each timeline as an ordered list with
`role="list"` and decorative numbers marked `aria-hidden="true"`.
Set `markdown="block"` on the list, each list item, and each step's content
container so paragraphs, links, code, and tables render inside the cards.
Keep decorative icons outside heading elements so the generated table of
contents contains only the step titles.

### Linking between pages

Use relative Markdown links between pages in `docs/`, for example
`[authentication](../authentication.md)` from a page under `docs/using/` or
`docs/contributing/`. Set `site_url` in `zensical.toml` once the documentation's
deployment URL is decided; no deployment URL is configured yet.

## Python dependencies

Zensical is declared in the `docs` dependency group in `pyproject.toml`,
resolved in `uv.lock` alongside the rest of the project's dependencies.
The preview and build commands below install only this locked group; they
do not require Docker or application dependencies. To install it explicitly:

```sh
uv sync --locked --only-group docs
```

## Previewing changes locally

```sh
make docs
```

This starts a live-reloading preview server on an available loopback port.
Open the `http://127.0.0.1:<port>` URL printed at startup and edit Markdown
files under `docs/`; the preview updates as you save. The port can change
between runs, so multiple previews do not need to share a fixed port.

## Building for production

```sh
make docs-build
```

This performs a strict build — it fails on broken links or other warnings
rather than ignoring them — and writes the static output to `site/`. Run this
before opening a pull request that touches documentation, and fix any
warnings it reports.

## Before opening a pull request

1. Confirm your page lives under the right audience section and is linked
   from the navigation in root `zensical.toml`.
2. Use relative links to other pages in `docs/`.
3. Run `make docs-build` and fix any warnings.
4. Follow the repository's general
   [contribution policy](https://github.com/cabotage/cabotage-app/blob/main/CONTRIBUTING.md)
   for branching, commits, and the pull request process.
