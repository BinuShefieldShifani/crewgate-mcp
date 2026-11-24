# ADR 0008: A backend-for-frontend console, and the whole stack behind one public URL

Status: accepted, 2026-11-24

## Context
Step 7 makes crewgate something a visitor can try: a web UI, and a live deployment, on free
hosting. Two decisions shape it: where the user's tokens live, and how a browser reaches four
services (console, Keycloak, gateway, Grafana) on platforms that give one public port.

## Decisions

**The console is a backend-for-frontend.** The browser never sees an access or refresh token.
`/login` runs the authorization code flow with PKCE as a confidential client (`crewgate-web`),
the server keeps the tokens in its session store, and the browser holds only an opaque HttpOnly
cookie (SameSite=Lax, Secure on https). The console verifies the token it receives the same way
the gateway does (issuer, audience, signature), and refreshes it before it expires. A single
page with no framework and no build step; a strict CSP (`script-src 'self'`), all text inserted
as text.

**Each turn shows its work.** The page shows every model call, tool call, refusal, rewrite and the
guard's decision, including the draft the guard replaced. That panel is the product demo: it's
how a reviewer sees the gateway and the guard working instead of taking the README's word.

**One public URL, Caddy in front.** `/auth` is Keycloak (`KC_HTTP_RELATIVE_PATH`), `/mcp` and
`/approvals` the gateway, `/grafana` dashboards, `/` the console. Keycloak's hostname is the public
URL, so tokens carry that issuer everywhere, while containers fetch keys and exchange tokens on the
internal network. Realm redirect URIs and client secrets come from environment placeholders
(`${CREWGATE_PUBLIC_URL:...}`), so one realm file serves local, CI, Codespaces and a server.
`/metrics` and Keycloak's admin console are 404 at the edge.

**Codespaces and a small VM, not a PaaS free tier.** In late 2026, Hugging Face made Docker
Spaces paid, Fly.io has no free tier, and the remaining free container tiers give 512 MB per
service, too little for Keycloak. A Codespace runs the full stack on demand; an Oracle Always
Free VM runs it all the time (Step 7b). Both use the same compose files, and CI tests that layout.

## Consequences
- Sessions are in memory: one console instance. Scaling out needs a shared store (Redis).
- A public demo spends the owner's free model quota, so the console caps it (20 messages per
  person per hour, 300 model calls a day overall).
- Demo users' passwords are public by design; anything that could cost money is test mode only.
