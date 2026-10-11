# Local development

This page is for people working **on Cabotage itself** — running the
application locally to change its code, not deploying an application onto a
Cabotage platform. For deploying apps onto Cabotage, see
[Using Cabotage](../using/index.md).

## Starting the stack

The development environment is managed with [Docker](https://www.docker.com/)
and [Compose](https://docs.docker.com/compose/). Install both before doing
anything else; they provide containers for the app and its dependencies.

```sh
make start
make migrate
make create-admin
```

- `make start` starts all containers needed to run the application, building
  images on first run.
- `make migrate` applies any pending database migrations. The application
  containers must already be running.
- `make create-admin` creates an admin user, organisation, and project. The
  admin user's ID is `ad@min.com` and its password is `admin`.

Alternatively, `make seed` runs `make migrate` and then seeds local data in
one step.

After these commands, visit `http://localhost:8000/` and log in with
`ad@min.com` / `admin`.

Use `CABOTAGE_DEBUG=True` for local development. Flask's `FLASK_DEBUG=1` or
`flask --debug run` alone does not bypass Cabotage's default-secret startup
validation. `make test` supplies test-only secrets with debug mode disabled.
See [startup validation](../operating/authentication.md#startup-validation) for commands
run outside debug mode.

Run `make help` (or just `make`) at any time for a full, sectioned list of
targets.

### Other stack commands

- `make rebuild` — rebuild the base application image and restart containers.
  Needed after changing `pyproject.toml`, `uv.lock`, or Docker configuration;
  most code changes show up without a rebuild.
- `make stop` — stop all containers, keeping volumes (database, caches,
  other persistent state).
- `make routes` — display all of the application's HTTP routes.

**Warning:** `make destroy` stops all containers and deletes their volumes,
including the database. Use `make stop` instead if you want to keep your local data.

### Migrations

Name a new migration with:

```sh
make migrations MESSAGE="add health probes"
```

This requires the application containers to be running.

## Python dependencies with uv

Dependencies are managed with [uv](https://docs.astral.sh/uv/), declared in
`pyproject.toml` and resolved in `uv.lock`.

```sh
make sync   # Install locked dependencies, including prek
make lock   # Regenerate uv.lock from pyproject.toml
```

To upgrade a single package:

```sh
uv lock --upgrade-package flask
```

Commit `pyproject.toml` and `uv.lock` together whenever dependency
declarations change.

## Git hooks with prek

Use [uv](https://docs.astral.sh/uv/) and [prek](https://prek.j178.dev/), not
the `pre-commit` Python package:

```sh
make sync           # Install dependencies from uv.lock, including prek
make hooks-install  # Install the Git pre-commit hook using prek
make hooks          # Run all hooks against all tracked files
```

To run hooks only against staged files:

```sh
uv run --locked prek run
```

Hooks need `uv` and Python, but not Docker. Ruff and Bandit check selected
Python files, and Ruff applies formatting. Type checks run when application
code or dependency files change; the test suite is not run as part of hooks.

To upgrade an individual tool:

```sh
uv lock --upgrade-package ruff
make sync
make hooks
```

## Checks and tests

Code checks and tests use Docker and Compose. Start the development services
once, then run:

```sh
make start
make ci
```

`make ci` runs **lint → fmt → test**, in that order, stopping at the first
failure even when Make is invoked with `-j`.

**Warning:** The `fmt` step in `make ci` writes formatting changes to your working
tree. Review and stage them before committing.

Individual targets:

```sh
make lint          # Check Python formatting and lint rules
make fmt           # Apply Ruff formatting
make type-check     # Run ty and pyrefly
make security-check # Run Bandit
make check          # lint + type-check
```

Tests require the Compose database and Redis services to be running; the test
target creates the test database and extensions, applies migrations, and then
runs pytest. Forward arguments to pytest with `ARGS`:

```sh
make ci ARGS="-k ingress --no-cov"
```

Lint also checks formatting — if it fails, run `make fmt` before `make ci`.

Tool commands can be overridden, for example:

```sh
make lint COMPOSE="docker compose -f docker-compose.yml"
```

## Before opening a pull request

Run `make ci` (and `make hooks` if you haven't installed the Git hook) so
that formatting, lint, type, and test checks match what CI runs. See the
[contribution policy](https://github.com/cabotage/cabotage-app/blob/main/CONTRIBUTING.md)
for branch, commit, and review conventions.
