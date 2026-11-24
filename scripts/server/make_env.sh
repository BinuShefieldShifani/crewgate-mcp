#!/usr/bin/env bash
# Create deploy/.env on the server: the domain, fresh random secrets, and your API keys.
# Run once, on the server only:   bash scripts/server/make_env.sh crewgate-binu.duckdns.org
# It asks for the Groq key and the Duffel test token without echoing them. The file is chmod 600
# and ignored by git. To rotate a secret, edit the file and run scripts/server/deploy.sh again.
set -euo pipefail
cd "$(dirname "$0")/../.."

domain="${1:?usage: make_env.sh <domain, e.g. crewgate-binu.duckdns.org>}"
out=deploy/.env
if [[ -e "$out" ]]; then
  echo "$out already exists; not overwriting it. Delete it first to start over." >&2
  exit 1
fi

secret() { openssl rand -hex 32; }

read -rsp "Groq API key (for the agent; Enter to leave chat off): " groq; echo
read -rsp "Duffel test token, duffel_test_... (Enter to leave booking off): " duffel; echo
if [[ -n "$duffel" && "$duffel" != duffel_test_* ]]; then
  echo "That is not a duffel_test_ token. crewgate only runs against Duffel's test mode." >&2
  exit 1
fi

umask 077
cat > "$out" <<ENV
# crewgate server settings. Secret: never commit, never paste anywhere.
CREWGATE_DOMAIN=${domain}
CREWGATE_PUBLIC_URL=https://${domain}
CREWGATE_PUBLIC_HOST=${domain}

KC_ADMIN_PASSWORD=$(secret)
GRAFANA_ADMIN_PASSWORD=$(secret)
CREWGATE_AUTH_CLIENT_SECRET=$(secret)
CREWGATE_APPROVALS_CLIENT_SECRET=$(secret)
CREWGATE_WEB_CLIENT_SECRET=$(secret)
CREWGATE_AUDIT_KEY=$(secret)
CREWGATE_PSEUDONYM_KEY=$(secret)
CREW_HMAC_SECRET=$(secret)
DUFFEL_WEBHOOK_SECRET=$(secret)

AGENT_LLM_API_KEY=${groq}
DUFFEL_ACCESS_TOKEN=${duffel}
ENV
chmod 600 "$out"
echo "Wrote $out (only you can read it). Next: bash scripts/server/deploy.sh <tag>"
