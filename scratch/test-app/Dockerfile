FROM alpine:3.17

COPY Procfile /Procfile

ARG BUILD_TESTING
ARG BUILD_TESTING_SECURE
ARG TESTING
ARG TESTING_SECURE

RUN --mount=type=cache,id=alpine-apk-3-17,target=/var/cache/apk,sharing=locked <<EOT
  #!/bin/sh
  ls /var/cache/apk/
  apk update
  apk add python3 bash
EOT

RUN ls /var/cache/apk

RUN echo $BUILD_TESTING
RUN echo $BUILD_TESTING_SECURE
RUN echo $TESTING
RUN echo $TESTING_SECURE
