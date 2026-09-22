FROM python:3.13-slim AS builder

ENV PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PIP_NO_CACHE_DIR=1

COPY requirements.txt /tmp/app-requirements.txt
COPY vendor/audio-forensic/requirements.txt /tmp/engine-requirements.txt

RUN python -m pip wheel \
      --wheel-dir /tmp/wheels \
      -r /tmp/app-requirements.txt \
      -r /tmp/engine-requirements.txt


FROM python:3.13-slim

ARG VERSION=0.1.0
ARG VCS_REF=unknown
ARG BUILD_DATE=unknown

LABEL org.opencontainers.image.title="SonicSentry" \
      org.opencontainers.image.description="Audio quality gate for FLAC integrity and forensic analysis" \
      org.opencontainers.image.source="https://github.com/SpaceChips31/lossless-validator" \
      org.opencontainers.image.version="${VERSION}" \
      org.opencontainers.image.revision="${VCS_REF}" \
      org.opencontainers.image.created="${BUILD_DATE}"

RUN apt-get update \
 && apt-get install -y --no-install-recommends \
      ffmpeg \
      flac \
      mediainfo \
      sox \
 && rm -rf /var/lib/apt/lists/* \
 && groupadd --gid 10001 validator \
 && useradd --uid 10001 --gid validator --create-home validator

COPY --from=builder /tmp/wheels /tmp/wheels
RUN python -m pip install --no-cache-dir /tmp/wheels/* \
 && rm -rf /tmp/wheels

COPY vendor/audio-forensic/audio_forensic.py \
     /opt/audio-forensic/audio_forensic.py

WORKDIR /app

COPY app/ /app/app/
COPY validator/ /app/validator/

RUN mkdir -p /app/data /app/reports /app/uploads \
 && chown -R validator:validator \
      /app /opt/audio-forensic

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    LOSSLESS_VALIDATOR_VERSION="${VERSION}" \
    LOSSLESS_VALIDATOR_DATA_DIR=/app/data \
    LOSSLESS_REPORT_ROOT=/app/reports \
    LOSSLESS_UPLOAD_ROOT=/app/uploads \
    LOSSLESS_SOURCE_ROOTS=/data/downloads/soulseek:/data/music/legacy

USER validator

EXPOSE 8090

HEALTHCHECK --interval=30s --timeout=5s --start-period=20s --retries=3 \
    CMD ["python", "-c", "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8090/health', timeout=3)"]

CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8090"]
