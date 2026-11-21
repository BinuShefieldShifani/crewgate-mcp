.PHONY: install lint fmt type test cov security check up down logs demo

install:      ## install all deps from the lockfile
	uv sync --frozen

lint:
	uv run ruff check .
	uv run ruff format --check .

fmt:
	uv run ruff check --fix .
	uv run ruff format .

type:
	uv run mypy

test:
	uv run pytest

cov:
	uv run pytest --cov

security:
	uv run bandit -ll -c pyproject.toml -r src services
	uv export --frozen --no-dev --no-hashes --no-emit-project --format requirements-txt > /tmp/req.txt
	uv run pip-audit -r /tmp/req.txt --progress-spinner off

check: lint type cov security   ## everything CI runs, except the image scan

up:
	docker compose -f deploy/docker-compose.yml up -d --build

down:
	docker compose -f deploy/docker-compose.yml down -v

logs:
	docker compose -f deploy/docker-compose.yml logs -f

demo:
	uv run python scripts/demo_crew_client.py
