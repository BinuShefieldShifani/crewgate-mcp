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

## Always on: a server

The same stack runs on an Oracle Cloud Always Free VM, with
`deploy/docker-compose.server.yml` on top of the public overlay:

- **TLS:** Caddy gets a Let's Encrypt certificate for the domain and renews it; HTTP redirects
  to HTTPS.
- **Keycloak:** production mode (`start`), with its data on a volume.
- **Images:** the release's signed images from GHCR, not built on the server.
- **Restarts:** every container restarts after a crash or a reboot; logs rotate.
- **Secrets:** every one comes from `deploy/.env`, made on the server by
  `scripts/server/make_env.sh` (random values, chmod 600, ignored by git). CI's
  **Server layout** job checks that no `dev-only-` value is left in the server's config.

### Once: the server

1. **Create the VM.** In the Oracle Cloud console, open Compute, Instances, Create instance.
   - Image: Canonical Ubuntu 24.04.
   - Shape: Ampere `VM.Standard.A1.Flex` with 2 OCPU and 12 GB, marked "Always Free eligible".
     If it says out of capacity, try another availability domain or try again later; 1 OCPU and
     6 GB also runs the stack.
   - Networking: a new virtual cloud network with a public subnet, and "Assign a public IPv4
     address" on.
   - SSH keys: "Generate a key pair for me", and download the private key.
2. **Open ports 80 and 443.** On the instance page, open the subnet, then its security list, and
   add two ingress rules: source `0.0.0.0/0`, TCP, destination port 80; the same for 443.
3. **Point the domain at it.** At duckdns.org, put the instance's public IP in your subdomain's
   "current ip" and press update. `ping crewgate-binu.duckdns.org` should answer from that IP.
4. **Log in and set up the server** (replace the key path and IP):
   ```bash
   chmod 600 ~/Downloads/ssh-key-*.key
   ssh -i ~/Downloads/ssh-key-*.key ubuntu@<public-ip>
   curl -fsSL https://raw.githubusercontent.com/BinuShefieldShifani/crewgate-mcp/main/scripts/server/bootstrap.sh -o bootstrap.sh
   less bootstrap.sh        # read what it does
   sudo bash bootstrap.sh   # Docker, firewall, updates, swap, the repo in /opt/crewgate
   exit                     # log out and in again, so the docker group applies
   ```
5. **Make the secrets file** (still on the server, after logging in again):
   ```bash
   cd /opt/crewgate
   bash scripts/server/make_env.sh crewgate-binu.duckdns.org
   ```
   It asks for the Groq key and the Duffel test token without showing them.

### Once: let GitHub deploy

1. On your Mac, make a key that only deploys: `ssh-keygen -t ed25519 -f ~/.ssh/crewgate_deploy -N ""`.
2. Put its public half on the server:
   `ssh -i ~/Downloads/ssh-key-*.key ubuntu@<public-ip> "cat >> ~/.ssh/authorized_keys" < ~/.ssh/crewgate_deploy.pub`
3. In the repo, Settings, Secrets and variables, Actions:
   - Secrets: `DEPLOY_HOST` (the public IP), `DEPLOY_USER` (`ubuntu`), `DEPLOY_SSH_KEY` (the
     contents of `~/.ssh/crewgate_deploy`, the private half), `DEPLOY_KNOWN_HOSTS` (the output
     of `ssh-keyscan -t ed25519 <public-ip>`).
   - Variables tab: `CREWGATE_DOMAIN` = `crewgate-binu.duckdns.org`.

### Every release

```bash
git checkout main && git pull
git tag v1.0.0 && git push origin v1.0.0
```

The **release** workflow builds the three images for amd64 and arm64, pushes them to GHCR with
an SBOM and build provenance, signs them with Sigstore, creates the GitHub release, then deploys
over SSH and checks the live site with the same browser-like test CI uses. On the server,
`scripts/server/deploy.sh` pulls that tag's images, starts them, waits for every health check and
the public URL, and puts the previous release back if anything fails.
`bash scripts/server/deploy.sh rollback` does that by hand.

The first time only: GHCR makes new packages private. After the images job, open your GitHub
profile, Packages, and for each of `crewgate-gateway`, `crewgate-web` and `crewgate-mock-roster`:
Package settings, Change visibility, Public. Then re-run the failed deploy job.

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
