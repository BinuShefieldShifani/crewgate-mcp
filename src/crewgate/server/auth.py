"""HTTP authentication for the gateway.

GatewayAuthMiddleware verifies any Bearer token on every request and puts the Principal on the
ASGI scope. It never refuses on its own: routes decide. RequirePrincipal guards the MCP endpoint
and answers missing or bad tokens with 401 and a WWW-Authenticate header that points at the
Protected Resource Metadata, as the MCP authorization spec requires.

We use our own verifier instead of the SDK's bearer backend because ours says *why* a token
failed (expired, wrong audience, untrusted issuer...), which goes into the response and metrics.
"""

import json

from mcp.server.auth.middleware.bearer_auth import AuthenticatedUser
from mcp.server.auth.provider import AccessToken
from starlette.types import ASGIApp, Receive, Scope, Send

from crewgate.core.auth import AuthError, Principal, TokenVerifier, www_authenticate
from crewgate.core.metrics import GatewayMetrics

PRINCIPAL = "crewgate.principal"
AUTH_ERROR = "crewgate.auth_error"


def principal_of(scope: Scope) -> Principal | None:
    p = scope.get(PRINCIPAL)
    return p if isinstance(p, Principal) else None


class GatewayAuthMiddleware:
    def __init__(self, app: ASGIApp, verifier: TokenVerifier, metrics: GatewayMetrics) -> None:
        self.app, self.verifier, self.metrics = app, verifier, metrics

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] == "http":
            header = next((v.decode() for k, v in scope["headers"] if k.lower() == b"authorization"), "")
            if header[:7].lower() == "bearer ":
                try:
                    p = await self.verifier.verify(header[7:].strip())
                except AuthError as exc:
                    scope[AUTH_ERROR] = exc
                    self.metrics.auth_failures.labels(exc.description).inc()
                else:
                    scope[PRINCIPAL] = p
                    # Also expose it the way the MCP SDK expects, so its session binding works.
                    scope["user"] = AuthenticatedUser(
                        AccessToken(
                            token=p.token.get_secret_value(),
                            client_id=p.client_id,
                            scopes=sorted(p.scopes),
                            expires_at=int(p.expires_at),
                            subject=p.subject,
                            resource=self.verifier.audience,
                            claims={"iss": self.verifier.issuer},
                        )
                    )
        await self.app(scope, receive, send)


async def send_auth_error(send: Send, metadata_url: str, error: AuthError | None) -> None:
    status = error.status if error else 401
    body = {"error": error.code.value if error else "invalid_token"}
    body["error_description"] = error.description if error else "authentication required"
    payload = json.dumps(body).encode()
    headers = [(b"content-type", b"application/json"), (b"content-length", str(len(payload)).encode())]
    if status in (401, 403):
        headers.append((b"www-authenticate", www_authenticate(metadata_url, error).encode()))
    if status == 503:
        headers.append((b"retry-after", b"5"))
    await send({"type": "http.response.start", "status": status, "headers": headers})
    await send({"type": "http.response.body", "body": payload})


class RequirePrincipal:
    def __init__(self, app: ASGIApp, metadata_url: str) -> None:
        self.app, self.metadata_url = app, metadata_url

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] == "http" and principal_of(scope) is None:
            await send_auth_error(send, self.metadata_url, scope.get(AUTH_ERROR))
            return
        await self.app(scope, receive, send)
