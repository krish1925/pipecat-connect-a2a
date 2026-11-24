FROM python:3.12-slim
COPY --from=ghcr.io/astral-sh/uv:latest /uv /usr/local/bin/uv
WORKDIR /app
COPY pyproject.toml uv.lock README.md LICENSE ./
COPY src ./src
COPY examples ./examples
RUN uv sync --frozen --extra examples --no-dev
ENV PATH="/app/.venv/bin:$PATH" PORT=8765
EXPOSE 8765
# Deploys: send SIGTERM; uvicorn drains. Connect reconnects once on close code 1012.
CMD ["sh", "-c", "uvicorn examples.bot:app --host 0.0.0.0 --port ${PORT}"]
