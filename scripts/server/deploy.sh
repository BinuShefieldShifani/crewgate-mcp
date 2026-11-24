#!/usr/bin/env bash
# Deploy one release on the server, or roll back to the last one that worked.
#
#   bash scripts/server/deploy.sh v1.0.0     # what the release workflow runs over SSH
#   bash scripts/server/deploy.sh rollback   # back to the previous good tag
#
# Checks out the tag, pulls its signed images from GHCR, verifies the signatures when cosign is
# installed, starts the stack, waits for every health check, then checks the public URL. If any
# of that fails, it puts the previous good release back.
set -euo pipefail
# The braces make bash read the whole script before running it: git checkout below replaces
# this very file, and bash would otherwise keep reading the new version halfway through.
{
cd "$(dirname "$0")/../.."

state=.deploy-state
files=(-f deploy/docker-compose.yml -f deploy/docker-compose.public.yml -f deploy/docker-compose.server.yml)
[[ -f deploy/.env ]] || { echo "deploy/.env is missing: run scripts/server/make_env.sh first" >&2; exit 1; }
# shellcheck disable=SC1091
set -a; source deploy/.env; set +a

current=$(grep '^good=' "$state" 2>/dev/null | cut -d= -f2 || true)
previous=$(grep '^previous=' "$state" 2>/dev/null | cut -d= -f2 || true)
tag="${1:?usage: deploy.sh <tag>|rollback}"
if [[ "$tag" == rollback ]]; then
  tag="${previous:?no previous release to roll back to}"
fi

start() {
  local t="$1"
  git fetch --quiet --tags --force origin
  git checkout --quiet --detach "$t"
  export CREWGATE_IMAGE_TAG="$t"
  docker compose "${files[@]}" pull --quiet mock-roster gateway web
  if command -v cosign >/dev/null; then
    for name in mock-roster gateway web; do
      cosign verify --certificate-oidc-issuer https://token.actions.githubusercontent.com \
        --certificate-identity-regexp '^https://github.com/BinuShefieldShifani/crewgate-mcp/' \
        "${CREWGATE_IMAGE_PREFIX:-ghcr.io/binushefieldshifani/crewgate}-$name:$t" >/dev/null
    done
    echo "signatures verified"
  fi
  docker compose "${files[@]}" up -d --no-build --remove-orphans --wait --wait-timeout 300
  for i in $(seq 1 30); do
    curl -fsS "https://${CREWGATE_DOMAIN}/healthz" >/dev/null && return 0
    sleep 2
  done
  echo "https://${CREWGATE_DOMAIN}/healthz did not answer" >&2
  return 1
}

if start "$tag"; then
  [[ "$tag" != "$current" ]] && printf 'good=%s\nprevious=%s\n' "$tag" "${current:-$tag}" > "$state"
  docker image prune -f >/dev/null
  echo "deployed $tag: https://${CREWGATE_DOMAIN}/"
else
  echo "deploy of $tag failed" >&2
  if [[ -n "$current" && "$current" != "$tag" ]]; then
    echo "rolling back to $current" >&2
    start "$current" && echo "rolled back to $current" >&2
  fi
  docker compose "${files[@]}" ps >&2 || true
  exit 1
fi
exit 0
}
