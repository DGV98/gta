# The gta-shorts watcher: Python + ffmpeg (yt-dlp needs it for ranged downloads).
FROM python:3.12-slim

RUN apt-get update \
    && apt-get install -y --no-install-recommends ffmpeg ca-certificates \
    && rm -rf /var/lib/apt/lists/*
COPY --from=ghcr.io/astral-sh/uv:0.11.3 /uv /usr/local/bin/uv

ENV UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    UV_PROJECT_ENVIRONMENT=/opt/venv \
    PYTHONUNBUFFERED=1 \
    HOME=/tmp
WORKDIR /src
COPY pyproject.toml uv.lock ./
RUN uv sync --frozen --no-dev --no-install-project
COPY src ./src
RUN uv sync --frozen --no-dev --no-editable

ENV PATH=/opt/venv/bin:$PATH
# config.toml and runs/ are mounted here (see docker-compose.yml).
WORKDIR /data
ENTRYPOINT ["gta-shorts", "-c", "/data/config.toml"]
CMD ["watch", "--quiet"]
