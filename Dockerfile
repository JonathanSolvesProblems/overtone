# Overtone hosted demo image.
#
# ffmpeg is a hard runtime dependency: transcode, frame extraction, mixing, and
# the freeze-frame path all shell out to it. The slim Python base does not ship
# it, so it is installed explicitly.
FROM python:3.12-slim

# ffmpeg (with ffprobe) for all media work.
RUN apt-get update \
    && apt-get install -y --no-install-recommends ffmpeg \
    && rm -rf /var/lib/apt/lists/*

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1 \
    PORT=8080

WORKDIR /app

# Install dependencies first for layer caching.
COPY pyproject.toml README.md ./
COPY src ./src
RUN pip install --upgrade pip && pip install ".[web]"

EXPOSE 8080

# A single worker: the guard's spend/rate state is process-local, and the
# demo's load is tiny. Scale by memory, not workers.
CMD ["overtone-web"]
