FROM python:3.12-slim AS runtime

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PATH="/app/.venv/bin:${PATH}"

WORKDIR /app

RUN apt-get update \
    && apt-get install -y --no-install-recommends ca-certificates git \
    && rm -rf /var/lib/apt/lists/* \
    && python -m pip install --no-cache-dir uv \
    && useradd --create-home --shell /usr/sbin/nologin app

COPY pyproject.toml uv.lock README.md ./
COPY src ./src

RUN uv sync --frozen --no-dev \
    && mkdir -p /var/lib/review-orchestrator/workspaces /var/lib/review-orchestrator/git-cache \
    && chown -R app:app /app /var/lib/review-orchestrator

USER app

EXPOSE 8000

CMD ["uvicorn", "review_orchestrator.presentation.main:app", "--host", "0.0.0.0", "--port", "8000"]
