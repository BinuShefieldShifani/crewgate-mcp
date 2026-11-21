# ADR 0001: Shared secure core with domain plugins

Status: accepted, 2026-11-21

## Context
The first version was one crew-roster client. A second domain (flight booking on Duffel) needs the same auth, rate limiting, logging and audit. Copying those per domain means security gets fixed in one place and forgotten in another.

## Decision
Split into `crewgate.core` (auth, entitlements, partner HTTP, logging, audit, metrics) and `crewgate.plugins.<domain>`. Plugins declare tools and required scopes through `ToolSpec`; the core wraps every tool call with the security pipeline. Tool names must be prefixed with the plugin name and unique.

## Consequences
- New domains add tools without touching security code.
- The core cannot import plugins. This is checked by keeping core free of domain names and by review.
- One more layer of indirection when reading a tool call end to end.
