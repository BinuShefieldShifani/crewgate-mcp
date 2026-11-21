# ADR 0002: Develop in GitHub Codespaces, run GPU evals on Kaggle

Status: accepted, 2026-11-21

## Context
The developer machine is an older Mac that cannot comfortably run Docker, Keycloak, the gateway and an LLM together. The system should not be cut down to fit it.

## Decision
- All coding, Docker Compose and integration runs happen in a GitHub Codespace defined by `.devcontainer/devcontainer.json` (2 cores, 8 GB, Docker-in-Docker).
- LLM evals that need a GPU run on Kaggle by calling `evals/*.py`; results are committed as JSON.
- CI on GitHub Actions is the source of truth for every check.

## Consequences
- Free tier covers 60 hours a month on a 2-core codespace; stop the codespace when not working.
- Duffel webhooks need a public HTTPS URL; Codespaces port forwarding can make port 8000 public for webhook demos.
