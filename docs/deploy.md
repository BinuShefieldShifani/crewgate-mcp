# Running crewgate where other people can use it

Two ways, same files: a GitHub Codespace for an on-demand demo, and a small always-on server.
Both put the whole stack behind **one public URL** with the Caddy edge
(`deploy/docker-compose.public.yml`, `deploy/edge/Caddyfile`):

| Path | Goes to |
|---|---|
| `/` | web console (sign-in, chat, the steps behind each reply) |
| `/auth/...` | Keycloak (sign-in pages, tokens) |
| `/mcp`, `/.well-known/oauth-protected-resource/mcp` | the MCP gateway, for any MCP client |
| `/approvals/...` | the gateway's approval pages |
| `/grafana/...` | dashboards, read-only for visitors |
| `/metrics`, `/auth/admin`, `/auth/realms/master` | 404: internal only |

One origin keeps it simple: the console's cookie, Keycloak's sign-in pages and the approval pages
all live on one URL, and Keycloak's issuer is that URL plus `/auth`. Containers still talk to
each other over the compose network (`http://keycloak:8080/auth`, `http://gateway:8000`).

The CI job **Public edge (Caddy, one URL)** starts exactly this layout on every pull request,
with `http://localhost:8443` as the public URL. It checks that no other port answers, signs in to
the console, asks the agent, and checks the blocked paths.

## On-demand: a Codespace

1. Add two **Codespaces** secrets in the repo (Settings, Secrets and variables, Codespaces):
   `GROQ_API_KEY` and `DUFFEL_TEST_TOKEN`. They are separate from the Actions secrets.
2. Open a Codespace on `main` (Code, Codespaces, Create codespace on main).
3. In its terminal: `make demo-public`. The first start takes about 3 minutes.
4. It prints the public URL. If it says the port is private, open the **Ports** tab, right-click
   port 8443, Port Visibility, Public.
5. Stop it with `bash scripts/codespace_demo.sh down`, and stop the Codespace when done: the free
   plan has a monthly allowance of core hours.

## Always on: a server (Step 7b)

An Oracle Cloud Always Free VM runs the same compose files with a real domain. Caddy gets the
TLS certificate itself, and GitHub Actions deploys each release over SSH. Real secrets live in
an `.env` file on the server, never in the repo. The `dev-only-...` values in the repo are
defaults for local runs and CI only.

## What a public demo protects

- Tokens stay on the server (backend-for-frontend); the browser gets an opaque HttpOnly cookie.
- POSTs need the console's own origin and a custom header (CSRF).
- 20 messages per person per hour, 300 model calls per day for everyone (the free model quota).
- When the main model's free daily quota runs out, the console switches to the next model
  (`AGENT_LLM_FALLBACK_MODELS`, default `openai/gpt-oss-20b`, which has its own quota) and shows
  which model answered each step. Evals never fall back: they measure one model.
- The gateway's own limits still apply per user and per app (`config/entitlements.toml`:
  the console is `crewgate-web`).
- Every write still waits for a person to sign in again and approve it; bookings run against
  Duffel's test mode only (a live token is refused at start).
