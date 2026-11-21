# Build from the repo root:  docker build -f services/mock_roster_api/Dockerfile -t crewgate/mock-roster .
FROM python:3.12-slim AS build
COPY --from=ghcr.io/astral-sh/uv:0.11 /uv /usr/local/bin/uv
WORKDIR /app
COPY pyproject.toml uv.lock README.md ./
COPY src ./src
COPY services ./services
RUN uv sync --frozen --no-dev --no-editable

FROM python:3.12-slim
# Pull in Debian security fixes released after the base image was built, then drop apt caches.
RUN apt-get update \
    && apt-get upgrade -y --no-install-recommends \
    && rm -rf /var/lib/apt/lists/* \
    && useradd --create-home --uid 10001 app
WORKDIR /app
COPY --from=build /app/.venv /app/.venv
ENV PATH="/app/.venv/bin:$PATH" MOCKAPI_HOST=0.0.0.0 MOCKAPI_PORT=8001
USER app
EXPOSE 8001
HEALTHCHECK --interval=10s --timeout=3s --retries=3 \
  CMD python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8001/health')"
CMD ["python", "-m", "mock_roster_api"]
