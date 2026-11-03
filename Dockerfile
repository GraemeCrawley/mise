FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    UV_LINK_MODE=copy \
    TZ=UTC

WORKDIR /app

# System deps:
#   libreoffice-{core,writer} — headless docx → PDF for recipe cards (Phase 2)
#   fonts-noto{,-cjk,-color-emoji} — recipe names include CJK + emoji
#   tzdata — APScheduler honours TZ
RUN apt-get update \
 && apt-get install -y --no-install-recommends \
      libreoffice-core libreoffice-writer \
      fonts-noto fonts-noto-cjk fonts-noto-color-emoji \
      tzdata \
 && rm -rf /var/lib/apt/lists/*

# uv from the official image — keeps Dockerfile small + reproducible.
COPY --from=ghcr.io/astral-sh/uv:latest /uv /usr/local/bin/uv

# Install deps first to cache the layer
COPY pyproject.toml uv.lock* ./
RUN uv sync --frozen --no-install-project 2>/dev/null \
 || uv sync --no-install-project  # accept un-locked first build

# Then the project
COPY . .
RUN uv sync --frozen 2>/dev/null || uv sync

EXPOSE 8000

# Migrate-then-serve. Alembic is idempotent (upgrade head on an
# already-current DB is a no-op), so this is safe to run on every
# container start.
CMD ["sh", "-c", "uv run alembic upgrade head && uv run uvicorn mise.main:app --host 0.0.0.0 --port 8000"]
