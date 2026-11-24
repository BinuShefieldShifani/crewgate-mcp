"""crewgate console: the web UI for the agent.

A backend-for-frontend (BFF): the browser holds only an HttpOnly session cookie. Sign-in is the
OAuth 2.1 authorization code flow with PKCE against Keycloak, done server-side by a confidential
client; access and refresh tokens never reach the browser. Each chat message runs the LangGraph
agent with the user's own token against the MCP gateway, and the page shows what happened
behind the reply: tool calls, blocked writes, rewrites, the guard's decision, approvals.
"""

from .app import create_app
from .settings import WebSettings

__all__ = ["WebSettings", "create_app"]
