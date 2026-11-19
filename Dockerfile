# syntax=docker/dockerfile:1.7
#
# Enduo Fitatu Agent: an unofficial agent for Enduo that sends the Fitatu food diary to Enduo.
# Not affiliated with Fitatu. MIT licence (LICENSE). Fitatu is read through fitatu-api
# (https://github.com/Jezue/fitatu_library, MIT).
#
# Every POLL_SECONDS it reads the recent days of the Fitatu diary and writes every day that changed to Enduo with
# the MCP tool nutrition_diary_set; on the first run it fills BACKFILL_DAYS of history, a batch per pass. Both
# sign-ins and the sync state live on the /data volume. It only connects out; no port is opened. How to run it:
# README.md
#
FROM python:3.15-slim-bookworm@sha256:96599df822d0e1c6bb700014a05c7f3c8cabc6e0f7ff3c4f9f69cee033b4b5d9

RUN apt-get update \
 && apt-get install -y --no-install-recommends tini qrencode ca-certificates \
 && rm -rf /var/lib/apt/lists/*

RUN groupadd -g 1000 enduo \
 && useradd -m -u 1000 -g 1000 -s /bin/sh enduo \
 && mkdir -p /data \
 && chown enduo:enduo /data \
 && chmod 700 /data

# Dependencies come from the hashed lock (requirements.txt: the runtime dependencies and the build backend), so pip
# refuses any file whose hash is not listed. The project is then built with what is installed and without an
# index: nothing outside the lock is downloaded.
COPY requirements.txt /opt/enduo-fitatu/
RUN pip install --no-cache-dir --root-user-action=ignore --require-hashes -r /opt/enduo-fitatu/requirements.txt
COPY pyproject.toml README.md LICENSE /opt/enduo-fitatu/
COPY src/ /opt/enduo-fitatu/src/
RUN pip install --no-cache-dir --root-user-action=ignore --no-index --no-deps --no-build-isolation /opt/enduo-fitatu \
 && pip check

ARG VERSION=dev
LABEL org.opencontainers.image.title="Enduo Fitatu Agent" \
      org.opencontainers.image.description="Unofficial agent for Enduo: sends the Fitatu food diary to Enduo" \
      org.opencontainers.image.source="https://github.com/sklimaszewski/enduo-fitatu-agent" \
      org.opencontainers.image.licenses="MIT" \
      org.opencontainers.image.version="${VERSION}"
ENV STATE_DIR=/data \
    ENDUO_URL=https://api.enduo.app \
    POLL_SECONDS=300 \
    BACKFILL_DAYS=90 \
    APP_VERSION=${VERSION} \
    PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1

USER enduo
WORKDIR /data
VOLUME ["/data"]

# Healthy while the service loop runs, signed in or not: a container that waits for its setup is not broken.
HEALTHCHECK --interval=60s --timeout=10s --start-period=30s --retries=3 \
  CMD enduo-fitatu health || exit 1

ENTRYPOINT ["tini", "--", "enduo-fitatu"]
CMD ["run"]
