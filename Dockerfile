# Optionnel (docker compose) — usage prévu : uv en local + main.py.
FROM python:3.10-slim-bookworm

COPY --from=ghcr.io/astral-sh/uv:latest /uv /usr/local/bin/uv

LABEL org.opencontainers.image.title="Malt Inbox" \
    org.opencontainers.image.description="Inbox locale Malt (sync SQLite, IA optionnelle)"

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    UV_LINK_MODE=copy \
    UV_COMPILE_BYTECODE=1 \
    PATH="/app/.venv/bin:$PATH"

WORKDIR /app

COPY pyproject.toml uv.lock README.md main.py ./
COPY malt_crm ./malt_crm/

RUN uv sync --frozen --no-dev

EXPOSE 8765

CMD ["python", "main.py"]
