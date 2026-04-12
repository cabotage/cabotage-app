FROM python:3.13-slim-trixie

ARG DEVEL no

# By default, Docker has special steps to avoid keeping APT caches in the layers, which
# is good, but in our case, we're going to mount a special cache volume (kept between
# builds), so we WANT the cache to persist.
RUN set -eux; \
    rm -f /etc/apt/apt.conf.d/docker-clean; \
    echo 'Binary::apt::APT::Keep-Downloaded-Packages "true";' > /etc/apt/apt.conf.d/keep-cache;

# Install System level build requirements, this is done before
# everything else because these are rarely ever going to change.
RUN --mount=type=cache,target=/var/cache/apt,sharing=locked \
    --mount=type=cache,target=/var/lib/apt,sharing=locked \
    set -x \
    && apt-get update \
    && apt-get install --no-install-recommends -y \
        git build-essential libffi-dev libpq-dev

COPY --from=moby/buildkit:v0.28.0-rootless /usr/bin/buildctl /usr/bin/buildctl
COPY --from=moby/buildkit:v0.28.0-rootless /usr/bin/buildkitd /usr/bin/buildkitd
COPY --from=moby/buildkit:v0.28.0-rootless /usr/bin/buildctl-daemonless.sh /usr/bin/buildctl-daemonless.sh
COPY --from=moby/buildkit:v0.28.0-rootless /usr/bin/buildkit-runc /usr/bin/buildkit-runc
COPY --from=moby/buildkit:v0.28.0-rootless /usr/bin/rootlesskit /usr/bin/rootlesskit
COPY --from=moby/buildkit:v0.28.0-rootless /usr/bin/fuse-overlayfs /usr/bin/fuse-overlayfs
COPY --from=moby/buildkit:v0.28.0-rootless /usr/bin/newuidmap /usr/bin/newuidmap
COPY --from=moby/buildkit:v0.28.0-rootless /usr/bin/newgidmap /usr/bin/newgidmap

ENV PYTHONUNBUFFERED=1
ENV UV_PROJECT_ENVIRONMENT=/opt/cabotage-app
ENV UV_COMPILE_BYTECODE=1
ENV UV_LINK_MODE=copy
ENV PATH="/opt/cabotage-app/bin:${PATH}"

COPY --from=ghcr.io/astral-sh/uv:0.11.2 /uv /usr/local/bin/uv

RUN uv venv /opt/cabotage-app

# Synchronize dependencies without the application itself.
# This layer is cached until uv.lock or pyproject.toml change.
COPY pyproject.toml uv.lock /opt/cabotage-app/src/
WORKDIR /opt/cabotage-app/src

RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --locked --no-install-project --no-editable \
    $(if [ "$DEVEL" != "yes" ]; then echo '--no-dev'; fi)

# Build and minify static assets for production
COPY --from=oven/bun:1-slim /usr/local/bin/bun /usr/local/bin/bun
COPY package.json bun.lock /opt/cabotage-app/src/
COPY cabotage/ /opt/cabotage-app/src/cabotage/
RUN if [ "$DEVEL" != "yes" ]; then \
    cd /opt/cabotage-app/src && \
    bun install --frozen-lockfile && \
    bun run build && \
    rm -rf node_modules; \
    fi

COPY migrations/ /opt/cabotage-app/src/migrations/
COPY gunicorn.conf.py /opt/cabotage-app/src/

