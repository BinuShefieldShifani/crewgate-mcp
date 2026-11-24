#!/usr/bin/env bash
# Start the whole stack behind one public URL in a GitHub Codespace.
#
#   bash scripts/codespace_demo.sh          # start (about 3 minutes the first time)
#   bash scripts/codespace_demo.sh down     # stop and delete the data
#
# Needs, as Codespaces secrets (repo Settings > Secrets and variables > Codespaces):
#   GROQ_API_KEY        the agent's model (without it the console runs with chat off)
#   DUFFEL_TEST_TOKEN   flight bookings (without it booking tools say "not configured")
set -euo pipefail

if [[ -z "${CODESPACE_NAME:-}" ]]; then
  echo "Not in a Codespace. Locally, use: make up  (console at http://localhost:8090)" >&2
  exit 1
fi

compose=(docker compose -f deploy/docker-compose.yml -f deploy/docker-compose.public.yml)
domain="${GITHUB_CODESPACES_PORT_FORWARDING_DOMAIN:-app.github.dev}"
export CREWGATE_PUBLIC_HOST="${CODESPACE_NAME}-8443.${domain}"
export CREWGATE_PUBLIC_URL="https://${CREWGATE_PUBLIC_HOST}"
export AGENT_LLM_API_KEY="${GROQ_API_KEY:-${AGENT_LLM_API_KEY:-}}"
export DUFFEL_ACCESS_TOKEN="${DUFFEL_TEST_TOKEN:-${DUFFEL_ACCESS_TOKEN:-}}"

if [[ "${1:-}" == "down" ]]; then
  "${compose[@]}" down -v
  exit 0
fi

[[ -n "$AGENT_LLM_API_KEY" ]] || echo "note: no GROQ_API_KEY secret, chat will be off"
[[ -n "$DUFFEL_ACCESS_TOKEN" ]] || echo "note: no DUFFEL_TEST_TOKEN secret, booking tools will say not configured"

"${compose[@]}" up -d --build --wait --wait-timeout 300

# A private port needs a GitHub sign-in; a public one works for anyone with the link.
if gh codespace ports visibility 8443:public -c "$CODESPACE_NAME" >/dev/null 2>&1; then
  visibility="public"
else
  visibility="private (to share it: Ports tab, right-click 8443, Port Visibility > Public)"
fi

cat <<MSG

crewgate is up. Port 8443 is ${visibility}.

  Console     ${CREWGATE_PUBLIC_URL}/
  Dashboards  ${CREWGATE_PUBLIC_URL}/grafana/d/crewgate
  MCP         ${CREWGATE_PUBLIC_URL}/mcp   (OAuth metadata at /.well-known/oauth-protected-resource/mcp)

  Demo users: anna / dev-only-anna (planner), erik / dev-only-erik (viewer)
  Stop it:    bash scripts/codespace_demo.sh down
MSG
