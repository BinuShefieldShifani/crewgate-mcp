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
| 3 | Tool guard: per-client entitlements, rate limits and quotas, HMAC-signed partner calls, Duffel-compatible webhook verification, redacted JSON logs, HMAC-chained audit log, Prometheus metrics | Done |
| 4 | MCP server (streamable HTTP, stateless, MCP SDK 2.3 low-level API): PRM, structured tools, out-of-band human approval of writes, raw JSON-RPC tests | Done |
| 5 | Booking plugin on Duffel: quirks, webhook signature verification | Next |
| 6 | "Never say confirmed" guard, LangGraph agent, tool-description variants, evals | |
| 7 | Kaggle eval run, dashboards, release | |

## Run it

Open in a GitHub Codespace (Code, Codespaces, Create codespace on main). Setup runs by itself. Then:

```bash
make check        # lint, strict mypy, unit tests with coverage, security scans
make up           # start Keycloak, mock crew-roster API and the MCP gateway, wait until healthy
make integration  # tests against real Keycloak: PKCE, token checks, OBO, roles
make demo         # Step 2: sign in, exchange, read, swap, and the refusals
make demo-guard   # Step 3: entitlements, rate limits, redacted logs, audit chain, metrics
make demo-mcp     # Step 4: the MCP server in raw JSON-RPC, and a write a human has to approve
make audit-verify # check the audit file's HMAC chain
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

## Connect an MCP client

The gateway serves MCP at `http://localhost:8000/mcp` (streamable HTTP). A client that follows the MCP
authorization spec finds everything itself: the 401 points at
`/.well-known/oauth-protected-resource/mcp`, which names Keycloak, which supports PKCE. In a Codespace you
can try it with the MCP Inspector: `npx @modelcontextprotocol/inspector`, then connect to the URL above.

| Tool | What it does |
|---|---|
| `crew_list_crew` | crew by base and rank (names, ranks, bases; no contact details) |
| `crew_get_roster` | one person's duties, optionally for a date range |
| `crew_find_cover` | standby crew first, then crew who are off, for a day and base |
| `crew_propose_swap` | **write**: creates an approval request, changes nothing yet |
| `gateway_approval_status` | pending, executed, rejected or expired, plus the result once done |

## Writes need a person, not just a token

`crew_propose_swap` returns `awaiting_approval` and a link. The planner opens it, sees in plain words
what will happen, and signs in **again** through a separate Keycloak client (`crewgate-approvals`,
`prompt=login`). The gateway only runs the write if that fresh sign-in is the same person and
happened in the last 5 minutes. The agent's own token is refused there. See
[ADR 0005](docs/adr/0005-human-approval-out-of-band.md).

## What every tool call goes through

`ToolGuard.run` is the only way to call a tool ([ADR 0004](docs/adr/0004-tool-guard-audit-and-logs.md)):

1. **Entitlement:** may this client app use this tool, in this market? (`config/entitlements.toml`, deny by default)
2. **Scope:** did the user grant the scope the tool needs?
3. **Rate limits:** per user per minute, per app per minute, per app per day, with `Retry-After` and `RateLimit-*` headers
4. **Run** the plugin, whose partner calls are OBO-authorised and HMAC-signed (method, path, body, timestamp, nonce)

Every outcome lands in the HMAC-chained audit log (arguments stored as a SHA-256 digest, never raw) and in
Prometheus metrics. Logs are JSON with tokens removed and personal data replaced by keyed pseudonyms.
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
