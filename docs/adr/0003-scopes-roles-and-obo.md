# ADR 0003: Scopes for the app, roles for the user, OBO to the partner

Status: accepted, 2026-11-21

## Context
An agent acts for a signed-in person (a crew planner). Three things must hold:
1. The partner API must see the real user, not a powerful gateway account.
2. A user must never be able to do more through the agent than they could do directly.
3. The user's token must not be usable anywhere except the gateway (MCP spec: no token passthrough).

## Decision
- **Sign-in:** the MCP client is a public Keycloak client using authorization code + PKCE (S256 only;
  "plain" and missing challenges are refused). Its token has `aud=crewgate-gateway`, the requested
  scopes, and no roles, names or emails (data minimisation, `fullScopeAllowed=false`).
- **Gateway:** verifies signature via JWKS, issuer, audience, expiry, and that it is an access token.
  Asymmetric algorithms only, and the token's `alg` must match the key's declared `alg`.
- **OBO:** the gateway (confidential client, Keycloak standard token exchange V2) swaps the user's
  token for one with `aud=crew-roster-api`, the same `sub`, and the user's client roles for that API.
  Partner tokens are cached per user, audience and scope set, and never outlive the user's token.
- **Partner:** requires the scope *and* the matching role (`roster.read` + `roster-reader`,
  `swap.write` + `swap-requester`). Scope is what the app may ask for; role is what the person may do.
  This mirrors Entra ID's split between delegated permissions (`scp`) and app roles (`roles`).

## Finding that shaped this
Keycloak 26.8 grants any optional scope of the requesting client during token exchange, even one the
user's token never had. A user who signed in with only `roster.read` can come back from the exchange
with `swap.write`. `test_keycloak_alone_would_upscope` pins this behaviour down.

So the gateway checks it itself: `TokenExchanger` refuses, before calling Keycloak, any scope that is
not already in the user's token (`test_gateway_refuses_to_upscope`). The partner's role check is the
second line: even with the scope, Erik (viewer) cannot swap (`test_viewer_cannot_swap_even_with_the_scope`).

## Consequences
- Two independent checks must both fail before a user gets extra power.
- Production would use Keycloak's client policies or fine-grained token exchange permissions as a
  third layer, and HTTPS everywhere (`sslRequired` is `none` only because this is a local dev realm).
