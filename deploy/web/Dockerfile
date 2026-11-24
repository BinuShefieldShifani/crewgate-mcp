# Build from the repo root:  docker build -f deploy/web/Dockerfile -t crewgate/web .
FROM python:3.12-slim AS build
COPY --from=ghcr.io/astral-sh/uv:0.11 /uv /usr/local/bin/uv
WORKDIR /app
COPY pyproject.toml uv.lock README.md ./
COPY src ./src
COPY services ./services
# The console runs the agent, so it needs the agent extra (LangGraph); the gateway image doesn't.
RUN uv sync --frozen --no-dev --no-editable --extra agent

FROM python:3.12-slim
RUN apt-get update \
    && apt-get upgrade -y --no-install-recommends \
    && rm -rf /var/lib/apt/lists/* \
    && useradd --create-home --uid 10001 app
WORKDIR /app
COPY --from=build /app/.venv /app/.venv
ENV PATH="/app/.venv/bin:$PATH" WEB_HOST=0.0.0.0 WEB_PORT=8090
USER app
EXPOSE 8090
HEALTHCHECK --interval=10s --timeout=3s --retries=3 \
  CMD python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8090/healthz')"
CMD ["python", "-m", "crewgate.web"]
