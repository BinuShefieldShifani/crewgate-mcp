# crewgate-mcp

[![ci](https://github.com/BinuShefieldShifani/crewgate-mcp/actions/workflows/ci.yml/badge.svg)](https://github.com/BinuShefieldShifani/crewgate-mcp/actions/workflows/ci.yml)

A shared, secure MCP core that lets AI agents work with real airline systems without leaking personal data, overstepping permissions or falling over when a partner API misbehaves. Two domain plugins prove it: **crew rosters** (mock partner API) and **flight booking** (Duffel test mode).

Connecting an agent to a live system is mostly an identity and data-handling problem. The AI part is the easy half. This repo is the other half.

Full design: [docs/DESIGN.md](docs/DESIGN.md)

## Status

| Step | Scope | State |
|---|---|---|
| 1 | Core + plugin structure, resilient partner client, mock crew-roster API, CI (lint, strict types, tests, security, image scan) | Done |
| 2 | Keycloak: JWT/JWKS verification, PKCE sign-in, OBO token exchange, M2M | Next |
| 3 | Entitlements, rate limits, HMAC signing, safe logging, audit chain, metrics | |
| 4 | MCP server + crew tools, human approval on writes, raw JSON-RPC tests, agent | |
| 5 | Booking plugin on Duffel: quirks, webhook signature verification | |
| 6 | "Never say confirmed" guard, tool-description variants, evals | |
| 7 | Kaggle eval run, dashboards, release | |

## Run it

Open in a GitHub Codespace (Code, Codespaces, Create codespace on main). Setup runs by itself. Then:

```bash
make check     # lint, strict mypy, tests with coverage, security scans
make up        # start the mock crew-roster API in Docker
make demo      # call it through the crew plugin client
```

## Layout

```
src/crewgate/core/       shared security and reliability (no domain code)
src/crewgate/plugins/    crew, booking
services/                mock partner APIs
deploy/                  docker compose stack
evals/                   tool-selection and guard evals
docs/                    design and ADRs
```

All crew data is generated and fictional.
