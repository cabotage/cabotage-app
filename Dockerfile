FROM python:3.8-slim-bullseye

RUN set -x \
    && apt-get update \
    && apt-get install --no-install-recommends -y \
        git build-essential libffi-dev libpq-dev

ENV PYTHONUNBUFFERED 1

RUN set -x \
    && python3 -m venv /opt/cabotage-app

ENV PATH="/opt/cabotage-app/bin:${PATH}"

RUN pip --no-cache-dir --disable-pip-version-check install --upgrade pip setuptools wheel

WORKDIR /opt/cabotage-app/src/

COPY requirements.txt requirements.txt
RUN pip install -r requirements.txt

COPY . /opt/cabotage-app/src/
