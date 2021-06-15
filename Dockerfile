FROM python:3.8-slim-buster

RUN set -x \
    && apt-get update \
    && apt-get install --no-install-recommends -y \
        git build-essential libffi-dev libpq-dev

ENV PYTHONUNBUFFERED 1

RUN set -x \
    && python3 -m venv /opt/cabotage-app

ENV PATH="/opt/cabotage-app/bin:${PATH}"

RUN pip --no-cache-dir --disable-pip-version-check install --upgrade pip setuptools wheel pipenv

COPY Pipfile /opt/cabotage-app/src/Pipfile
COPY Pipfile.lock /opt/cabotage-app/src/Pipfile.lock

WORKDIR /opt/cabotage-app/src/

RUN pipenv install --dev --deploy

COPY . /opt/cabotage-app/src/
