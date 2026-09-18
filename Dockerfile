FROM python:3.13-slim

RUN apt-get update \
 && apt-get install -y --no-install-recommends \
      ffmpeg \
      flac \
      mediainfo \
      sox \
 && rm -rf /var/lib/apt/lists/*

COPY requirements.txt /tmp/app-requirements.txt
COPY vendor/audio-forensic/requirements.txt /tmp/engine-requirements.txt

RUN pip install --no-cache-dir \
      -r /tmp/app-requirements.txt \
      -r /tmp/engine-requirements.txt

COPY vendor/audio-forensic/audio_forensic.py \
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
