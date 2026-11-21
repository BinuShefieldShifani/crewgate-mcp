.PHONY: install lint fmt type test cov security check up down logs demo demo-m2m demo-guard demo-mcp gateway audit-verify integration

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

up:           ## start Keycloak, mock crew API and the gateway, wait until all are healthy
	docker compose -f deploy/docker-compose.yml up -d --build --wait --wait-timeout 240

down:
	docker compose -f deploy/docker-compose.yml down -v

logs:
	docker compose -f deploy/docker-compose.yml logs -f

demo:         ## Step 2: PKCE sign-in, OBO exchange, role enforcement (needs make up)
	uv run python scripts/demo_obo.py

demo-mcp:     ## Step 4: the MCP server over raw JSON-RPC, write approval by a human (needs make up)
	uv run python scripts/demo_mcp.py

gateway:      ## run the gateway on your machine instead of in Docker (stack minus gateway must be up)
	uv run python -m crewgate.server

demo-guard:   ## Step 3: entitlements, scopes, rate limits, signing, redacted logs, audit chain, metrics
	uv run python scripts/demo_guard.py

audit-verify: ## check the HMAC chain of the audit file written by demo-guard
	uv run python scripts/verify_audit.py var/audit.jsonl

demo-m2m:     ## Step 1: client credentials against a mock API in local mode (no Keycloak)
	uv run python scripts/demo_crew_client.py

integration:  ## tests against the running stack (needs make up)
	uv run pytest -m integration tests/integration -v
