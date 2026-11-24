# ADR 0009: Signed multi-arch releases, deployed over SSH with a rollback

Status: accepted, 2026-11-24

## Context
The public demo needs to be up when a reviewer opens the link, on free infrastructure, and
every version running there should be traceable to a commit and a CI run.

## Decisions

**One small VM, the same compose files.** No Kubernetes: one node, one tenant, a demo. The server
runs exactly the layout CI starts in the **Server layout** job, plus a domain. Keycloak runs in
production mode with its embedded database on a volume; the realm file is the source of truth
for clients and demo users.

**Releases are tags.** A `v*.*.*` tag builds the gateway, console and mock API images for
linux/amd64 and linux/arm64 (Oracle's free VMs are Arm), pushes them to GHCR with BuildKit's SBOM
and SLSA provenance attached, and signs each by digest with Sigstore keyless signing, so the
signature names this repository's release workflow and nothing else. The server only pulls
images; it never builds.

**Deploys go over SSH with a pinned host key**, to a key that does nothing but log in as the
deploy user. `scripts/server/deploy.sh` checks out the tag, verifies the signatures when cosign
is installed, starts the stack, waits for every health check and for the public `/healthz`, and
otherwise starts the previous good tag again. The workflow then runs the browser-like console
test against the live URL.

**Secrets live on the server only.** `make_env.sh` generates every secret with
`openssl rand -hex 32` into `deploy/.env` (chmod 600, git-ignored); the Groq and Duffel keys
are typed in, never echoed. GitHub holds only the SSH key and the host's public key.

## Consequences
- A deploy restarts containers: seconds of downtime. Fine for a demo; a second node and a load
  balancer would remove it.
- The H2 file database is single-node and not for high traffic, which this isn't.
- New GHCR packages start private; making them public once is a manual step.
