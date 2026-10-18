# syntax=docker/dockerfile:1
FROM python:3.11-slim AS base

# ── Security: run as non-root ──────────────────────────────────────────────────
RUN groupadd --gid 1001 appgroup \
    && useradd --uid 1001 --gid appgroup --no-create-home --shell /bin/false appuser

WORKDIR /app

ENV PYTHONUNBUFFERED=1
ENV PYTHONPATH=/app

# ── Dependencies ───────────────────────────────────────────────────────────────
COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt

# ── Application source ─────────────────────────────────────────────────────────
COPY src/ ./src/

# ── Data directory (WAL + snapshots) ──────────────────────────────────────────
RUN mkdir -p /app/data && chown -R appuser:appgroup /app

USER appuser

# ── Entrypoint ─────────────────────────────────────────────────────────────────
CMD ["python", "src/main.py"]
