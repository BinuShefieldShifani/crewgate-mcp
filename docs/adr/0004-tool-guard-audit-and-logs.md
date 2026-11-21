# ADR 0004: One guard for every tool call, a keyed audit chain, pseudonymised logs

Status: accepted, 2026-11-21

## Context
Agents call tools, tools call partners. If any tool can skip a check, the checks don't count.
Auditors need to know who did what, and GDPR says logs must not hold more personal data than needed.

## Decisions

**One enforcement point.** `ToolGuard.run` is the only path to a plugin handler. The order is
entitlement, then scope, then rate limits, then run. Refusals never reach the plugin and never spend
rate-limit budget. Plugins can't opt out: they only declare a `ToolSpec`.

**Entitlements per client app**, in `config/entitlements.toml`, deny by default. Scopes come from the
user's consent; entitlements are our contract with each app (which tools, which markets, how much).
Market-bound tools must name a market.

**Three rate limits**: per user per minute, per app per minute, per app per UTC day. Token buckets, so
no burst at the top of a minute. In memory for now; several replicas would need Redis behind the same
interface.

**Audit log chained with HMAC-SHA256, not plain SHA-256.** With a plain hash, anyone who can edit the
file can recompute the chain. With a keyed MAC they can't. Arguments are stored as a SHA-256 digest:
enough to prove later which exact request was made, without storing names or crew ids. Each record is
fsynced. Publishing the chain head elsewhere anchors it against truncation.

**Logs: remove secrets, pseudonymise people.** Tokens, passwords and signatures become `[REDACTED]`.
Names, emails, phones, birth dates and crew or passenger ids become `pii:<12 hex>`, a keyed HMAC, so one
person's requests can still be followed through the logs without exposing who they are.

**Metrics with bounded labels**: tool, client app, outcome, reason. Never a user or crew id.

**Signatures**: outbound partner calls sign timestamp, nonce, method, path and body; the partner keeps
seen nonces for the tolerance window. Inbound webhooks follow Duffel's scheme exactly, plus a
timestamp window and duplicate detection, because Duffel retries failed deliveries.

## Consequences
- Step 4's MCP server calls only `ToolGuard.run`; its tests can assert nothing else is reachable.
- Audit and pseudonym keys are secrets and need rotation like any other (Key Vault in production).
- In-memory nonce, rate and replay stores reset on restart; acceptable for one replica, documented for more.
