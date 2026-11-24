.PHONY: install lint fmt type test cov security check up down logs demo demo-m2m demo-guard demo-mcp demo-booking contract gateway audit-verify integration eval-offline eval-selection eval-scenarios agent web demo-web demo-public

install:      ## install all deps from the lockfile
	uv sync --frozen --all-extras

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
	uv export --frozen --no-dev --all-extras --no-hashes --no-emit-project --format requirements-txt > /tmp/req.txt
	uv run pip-audit -r /tmp/req.txt --progress-spinner off

check: lint type cov security   ## everything CI runs, except the image scan

up:           ## start Keycloak, mock API, gateway, web console, Prometheus, Grafana; wait until healthy
	docker compose -f deploy/docker-compose.yml up -d --build --wait --wait-timeout 240

down:
	docker compose -f deploy/docker-compose.yml down -v

logs:
	docker compose -f deploy/docker-compose.yml logs -f

demo:         ## Step 2: PKCE sign-in, OBO exchange, role enforcement (needs make up)
	uv run python scripts/demo_obo.py

demo-mcp:     ## Step 4: the MCP server over raw JSON-RPC, write approval by a human (needs make up)
	uv run python scripts/demo_mcp.py

demo-booking: ## Step 5: book on Duffel's real test API through the gateway (make up with DUFFEL_ACCESS_TOKEN set)
	uv run python scripts/demo_booking.py

contract:     ## contract tests against Duffel's real test API (needs DUFFEL_ACCESS_TOKEN=duffel_test_...)
	uv run pytest -m sandbox tests/contract -v -s

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

eval-offline: ## Step 6: guard corpus, attacks, Duffel quirks, gateway overhead. No model, ~30 s
	uv run python -m evals.run offline

eval-selection: ## Step 6: tool-selection accuracy per description variant (AGENT_LLM_API_KEY = your Groq key)
	uv run python -m evals.run selection --budget 110

eval-scenarios: ## Step 6: agent end-to-end on Duffel's quirk routes (AGENT_LLM_API_KEY = your Groq key)
	uv run python -m evals.run scenarios --budget 70

agent:        ## Step 6: chat with the agent against the running stack (make up, AGENT_LLM_API_KEY set)
	uv run python scripts/agent_chat.py

web:          ## Step 7: run the web console locally on :8090 (make up for everything else)
	uv run python -m crewgate.web

demo-web:     ## Step 7: sign in to the console like a browser and ask the agent (make up first)
	uv run python scripts/demo_web.py

demo-public:  ## Step 7: in a Codespace, the whole stack behind one public URL
	bash scripts/codespace_demo.sh
