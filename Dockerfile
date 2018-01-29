FROM python:3.6-slim-stretch

RUN set -x \
    && apt-get update \
    && apt-get install --no-install-recommends -y \
        git

ENV PYTHONUNBUFFERED 1

RUN set -x \
    && python3 -m venv /opt/cabotage-app

ENV PATH="/opt/cabotage-app/bin:${PATH}"

RUN pip --no-cache-dir --disable-pip-version-check install --upgrade pip setuptools wheel pipenv

COPY . /opt/cabotage-app/src/

WORKDIR /opt/cabotage-app/src/

RUN pipenv install --dev
