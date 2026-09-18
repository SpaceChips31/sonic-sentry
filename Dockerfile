FROM alpine/git:2.49.1 AS forensic-engine

ARG AUDIO_FORENSIC_COMMIT=b10cab6f29e9a43677e3adae97a7c39e0793268

RUN git clone https://github.com/spideyonmoon/audio-forensic.git /engine \
 && cd /engine \
 && git checkout "${AUDIO_FORENSIC_COMMIT}"


FROM python:3.13-slim

RUN apt-get update \
 && apt-get install -y --no-install-recommends \
      ffmpeg \
      flac \
      mediainfo \
      sox \
 && rm -rf /var/lib/apt/lists/*

COPY requirements.txt /tmp/app-requirements.txt
COPY --from=forensic-engine /engine/requirements.txt /tmp/engine-requirements.txt

RUN pip install --no-cache-dir \
      -r /tmp/app-requirements.txt \
      -r /tmp/engine-requirements.txt

COPY --from=forensic-engine \
     /engine/audio_forensic.py \
     /opt/audio-forensic/audio_forensic.py

WORKDIR /app

COPY app/ /app/app/
COPY validator/ /app/validator/

ENV PYTHONUNBUFFERED=1
ENV LOSSLESS_VALIDATOR_DATA_DIR=/app/data
ENV LOSSLESS_REPORT_ROOT=/app/reports
ENV LOSSLESS_UPLOAD_ROOT=/app/uploads
ENV LOSSLESS_SOURCE_ROOTS=/data/downloads/soulseek:/data/music/legacy

CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8090"]
