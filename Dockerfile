FROM python:3.11-slim-bullseye

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

COPY --from=moby/buildkit:v0.11.3-rootless /usr/bin/buildctl /usr/bin/buildctl

ENV PYTHONUNBUFFERED 1

RUN set -x \
    && python3 -m venv /opt/cabotage-app

ENV PATH="/opt/cabotage-app/bin:${PATH}"

RUN pip --no-cache-dir --disable-pip-version-check install --upgrade pip setuptools wheel

WORKDIR /opt/cabotage-app/src/

COPY requirements.txt requirements.txt
RUN --mount=type=cache,target=/root/.cache/pip \
    pip install -r requirements.txt

COPY . /opt/cabotage-app/src/

USER nobody
