# Lean pilot worker: run whatever collection or scoring is due, then exit.
#
# Built and smoke-tested by .github/workflows/worker-image.yml. Building this image
# provisions nothing; see docs/railway-deployment.md for the scheduled service.
# Build context is the repository root:
#   docker build -f deploy/worker.Dockerfile -t marketsignalos-worker .
FROM python:3.13-slim
COPY --from=ghcr.io/astral-sh/uv:0.12.3 /uv /usr/local/bin/uv

ENV UV_PROJECT_ENVIRONMENT=/opt/venv \
    UV_PYTHON_DOWNLOADS=never \
    UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    PATH=/opt/venv/bin:$PATH \
    PYTHONUNBUFFERED=1 \
    PYTHONPATH=/app/.migration-backup/services/polymarket-ingestor/src

WORKDIR /app

# Production dependencies only, from the committed lock. The dev group, which
# installs both packages editable, is not needed: the worker imports the
# ingestor from PYTHONPATH, exactly as CI and the scheduled workflows do.
COPY pyproject.toml uv.lock ./
RUN uv sync --frozen --no-dev --no-install-project && rm /usr/local/bin/uv

COPY .migration-backup/services/polymarket-ingestor/src .migration-backup/services/polymarket-ingestor/src
COPY deploy/lean-pilot.json deploy/start-worker.sh deploy/

ENTRYPOINT ["bash", "/app/deploy/start-worker.sh"]
CMD ["--run"]
