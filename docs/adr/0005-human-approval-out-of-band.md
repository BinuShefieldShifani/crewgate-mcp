# ADR 0005: Human approval of writes happens outside MCP, with a fresh sign-in

Status: accepted, 2026-11-21

## Context
Write tools (a duty swap now, a flight booking in Step 5) must not run on an agent's say-so. MCP
offers form elicitation for asking the user something mid-call, but the request goes to the MCP
client, and the MCP Python SDK's own docs note that an agent client may answer it itself. An
approval the agent can give itself is no approval.

## Decision
- A write tool call creates a `PendingAction` (10 minute lifetime) and returns
  `{"status": "awaiting_approval", "approval_id", "summary", "approve_url"}`. Nothing else happens:
  not even a token exchange.
- The person opens `approve_url`, reads a one-line summary written by the tool's `summarize()`,
  and clicks Approve or Reject. That starts a new authorization-code + PKCE sign-in with a separate
  confidential Keycloak client, `crewgate-approvals`, with `prompt=login` and `max_age=0`.
- The gateway runs the write only if the approval token:
  - comes from `crewgate-approvals` (the agent's `crewgate-mcp-client` token is refused),
  - belongs to the same person who was signed in when the agent proposed it,
  - has an `auth_time` less than 5 minutes old,
  - carries the tool's scope.
- The write runs with the approver's own fresh token (OBO), asking for that one scope only. The
  partner still checks roles, so a viewer who approves their own request is still refused.
- `run()` refuses write tools outright, and the plugin loader refuses a write tool with no
  `summarize()`. There is no path from an agent to a write without a person.
- `gateway.approval_status` lets the agent report the truth: pending, executed, rejected, expired.
- Every step (proposed, refused approver, approved or rejected, outcome) is in the audit chain.

## Alternatives considered
- **Form elicitation:** rejected, the agent may answer it.
- **URL elicitation** (`UrlElicitationRequiredError`): fits the idea, but only clients that support
  it would see the link. A structured result works with every client, and a client that supports
  URL elicitation can still open `approve_url`.
- **Approval with the agent's own token:** rejected, it proves nothing about the person.

## Consequences
- Pending actions and login states live in memory: one replica, or a shared store for more.
- Approval pages are capability URLs (unguessable id) and show crew ids, not names.
- A real deployment would add step-up authentication (Keycloak ACR levels, MFA) to the approval
  client: the design already forces a fresh sign-in, so that is configuration, not code.
