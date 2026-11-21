# crewgate-mcp

[![ci](https://github.com/BinuShefieldShifani/crewgate-mcp/actions/workflows/ci.yml/badge.svg)](https://github.com/BinuShefieldShifani/crewgate-mcp/actions/workflows/ci.yml)
[![integration](https://github.com/BinuShefieldShifani/crewgate-mcp/actions/workflows/integration.yml/badge.svg)](https://github.com/BinuShefieldShifani/crewgate-mcp/actions/workflows/integration.yml)

A shared, secure MCP core that lets AI agents work with real airline systems without leaking personal data, overstepping permissions or falling over when a partner API misbehaves. Two domain plugins prove it: **crew rosters** (mock partner API) and **flight booking** (Duffel test mode).

Connecting an agent to a live system is mostly an identity and data-handling problem. The AI part is the easy half. This repo is the other half.

Full design: [docs/DESIGN.md](docs/DESIGN.md)

## Status

| Step | Scope | State |
|---|---|---|
| 1 | Core + plugin structure, resilient partner client, mock crew-roster API, CI (lint, strict types, tests, security, image scan) | Done |
| 2 | Keycloak: JWT/JWKS verification, PRM (RFC 9728), PKCE sign-in, OBO token exchange (RFC 8693), integration CI | Done |
| 3 | Entitlements, rate limits, HMAC signing, safe logging, audit chain, metrics | Next |
| 4 | MCP server + crew tools, human approval on writes, raw JSON-RPC tests, agent | |
| 5 | Booking plugin on Duffel: quirks, webhook signature verification | |
| 6 | "Never say confirmed" guard, tool-description variants, evals | |
| 7 | Kaggle eval run, dashboards, release | |

## Run it

Open in a GitHub Codespace (Code, Codespaces, Create codespace on main). Setup runs by itself. Then:

```bash
make check        # lint, strict mypy, unit tests with coverage, security scans
make up           # start Keycloak + mock crew-roster API, wait until healthy
make integration  # tests against real Keycloak: PKCE, token checks, OBO, roles
make demo         # watch it: sign in, exchange, read, swap, and the refusals
make down         # stop and remove the stack
```

Keycloak admin console: http://localhost:8080 (admin / dev-only-admin). Demo users: `anna` (planner, may swap)
and `erik` (viewer, read only). All passwords in this repo are dev-only placeholders.

## How a request is authorised

```
MCP client --(auth code + PKCE)--> Keycloak --> token: aud=crewgate-gateway, scopes, no roles, no PII
MCP client --(Bearer)--> crewgate gateway: verify signature (JWKS), issuer, audience, expiry, type
crewgate --(RFC 8693 token exchange, only scopes the user already has)--> Keycloak
         --> token: aud=crew-roster-api, same user, the user's roles
crewgate --(OBO token)--> partner API: needs the scope AND the user's role
```

A scope says what the app may ask for. A role says what this person may do. The partner needs both,
so an over-permissive app registration can't let a viewer make changes. See
[ADR 0003](docs/adr/0003-scopes-roles-and-obo.md) for why the gateway also blocks up-scoping itself.
```

## Layout

```
src/crewgate/core/       shared security and reliability (no domain code)
src/crewgate/core/auth/  JWT/JWKS verifier, PRM, OBO token exchange
src/crewgate/clientauth/ MCP client side: discovery, PKCE
src/crewgate/plugins/    crew, booking
services/                mock partner APIs
deploy/                  docker compose stack, Keycloak realm
evals/                   tool-selection and guard evals
docs/                    design and ADRs
```

All crew data is generated and fictional.
