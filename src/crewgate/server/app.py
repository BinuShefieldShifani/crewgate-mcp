"""Assemble the crewgate MCP gateway: one Starlette app.

  /mcp                                     MCP over streamable HTTP (stateless, JSON responses)
  /.well-known/oauth-protected-resource/mcp  Protected Resource Metadata (RFC 9728)
  /approvals/...                           human approval of write tools
  /metrics                                 Prometheus
  /healthz                                 liveness

Stateless mode: every request carries its own token and no session state lives in the gateway,
so replicas can sit behind any load balancer. It is also the mode the 2026-07-28 MCP spec
expects for streamable HTTP.
"""

from collections.abc import AsyncIterator, Callable
from contextlib import asynccontextmanager
from dataclasses import dataclass
from pathlib import Path

import httpx
from mcp.server.lowlevel import Server
from mcp.server.streamable_http_manager import StreamableHTTPASGIApp, StreamableHTTPSessionManager
from mcp.server.transport_security import TransportSecuritySettings
from starlette.applications import Starlette
from starlette.middleware import Middleware
from starlette.requests import Request
from starlette.responses import JSONResponse, Response
from starlette.routing import Route

import crewgate
from crewgate.core.approvals import ApprovalStore
from crewgate.core.audit import AuditLog, FileSink
from crewgate.core.auth import (
    AuthSettings,
    JwksCache,
    TokenExchanger,
    TokenVerifier,
    metadata_path,
    metadata_url,
    protected_resource_metadata,
)
from crewgate.core.entitlements import EntitlementPolicy
from crewgate.core.guard import ToolGuard
from crewgate.core.metrics import GatewayMetrics
from crewgate.core.plugin import PluginContext, ToolSpec, load_plugins
from crewgate.core.ratelimit import RateLimiter

from .approvals_web import ApprovalsWeb
from .auth import GatewayAuthMiddleware, RequirePrincipal
from .mcp_tools import ToolRouter
from .settings import GatewaySettings

INSTRUCTIONS = (
    "crewgate gives you airline crew-roster tools. Read tools answer straight away. Write tools "
    "(names containing 'propose') only create a request a human must approve in their browser: "
    "say it is awaiting approval and use gateway_approval_status to check before claiming it is done."
)


@dataclass
class Gateway:
    settings: GatewaySettings
    auth: AuthSettings
    verifier: TokenVerifier
    exchanger: TokenExchanger
    guard: ToolGuard
    metrics: GatewayMetrics
    specs: dict[str, ToolSpec]
    router: ToolRouter
    http: httpx.AsyncClient


def create_app(
    settings: GatewaySettings | None = None,
    auth: AuthSettings | None = None,
    *,
    idp_http: httpx.AsyncClient | None = None,
    partner_transport: httpx.AsyncBaseTransport | None = None,
    audit_sink: object | None = None,
    clock: Callable[[], float] | None = None,
) -> tuple[Starlette, Gateway]:
    """`idp_http`, `partner_transport` and `audit_sink` are seams for tests."""
    s = settings or GatewaySettings()
    a = auth or AuthSettings()
    http = idp_http or httpx.AsyncClient(timeout=10.0)
    metrics = GatewayMetrics()
    verifier = TokenVerifier(
        a.issuer, a.audience, JwksCache(a.jwks_url, http, ttl_seconds=a.jwks_ttl_seconds)
    )
    exchanger = TokenExchanger(http, a.token_url, a.client_id, a.client_secret, metrics=metrics)
    approvals = ApprovalStore(ttl_seconds=s.approval_ttl_seconds, **({"clock": clock} if clock else {}))
    guard = ToolGuard(
        EntitlementPolicy.from_toml(s.entitlements_path),
        RateLimiter(**({"clock": clock} if clock else {})),
        AuditLog(s.audit_key, audit_sink or FileSink(Path(s.audit_path))),  # type: ignore[arg-type]
        metrics,
        approvals,
        approval_clients=[s.approvals_client_id],
        max_sign_in_age_seconds=s.max_sign_in_age_seconds,
        **({"clock": clock} if clock else {}),
    )
    ctx = PluginContext(
        exchanger=exchanger, metrics=metrics, approvals=approvals, partner_transport=partner_transport
    )
    plugins = load_plugins(["gateway", *[p for p in s.plugins if p != "gateway"]], ctx)
    specs = {spec.name: spec for plugin in plugins for spec in plugin.tools()}
    router = ToolRouter(list(specs.values()), guard, s.public_url)

    server = Server(
        "crewgate",
        version=getattr(crewgate, "__version__", "0"),
        instructions=INSTRUCTIONS,
        on_list_tools=router.list_tools,
        on_call_tool=router.call_tool,
    )
    sessions = StreamableHTTPSessionManager(
        app=server,
        json_response=True,
        stateless=True,
        security_settings=TransportSecuritySettings(
            enable_dns_rebinding_protection=True,
            allowed_hosts=s.allowed_hosts,
            allowed_origins=s.allowed_origins,
        ),
    )
    gateway = Gateway(s, a, verifier, exchanger, guard, metrics, specs, router, http)
    web = ApprovalsWeb(guard, specs, verifier, http, s, a, **({"clock": clock} if clock else {}))

    prm = protected_resource_metadata(
        a.resource,
        [a.issuer],
        sorted({spec.required_scope for spec in specs.values()}),
        resource_name="crewgate MCP gateway",
        resource_documentation="https://github.com/BinuShefieldShifani/crewgate-mcp",
    )

    async def metadata(_: Request) -> Response:
        return JSONResponse(prm, headers={"Cache-Control": "max-age=300"})

    async def health(_: Request) -> Response:
        return JSONResponse({"status": "ok"})

    async def prometheus(_: Request) -> Response:
        return Response(metrics.exposition(), media_type="text/plain; version=0.0.4")

    @asynccontextmanager
    async def lifespan(_: Starlette) -> AsyncIterator[None]:
        async with sessions.run():
            yield
        await http.aclose()

    mcp_endpoint = RequirePrincipal(StreamableHTTPASGIApp(sessions), metadata_url(a.resource))
    routes = [
        Route(s.mcp_path, mcp_endpoint, methods=["GET", "POST", "DELETE"]),
        Route(metadata_path(a.resource), metadata, methods=["GET"]),
        Route("/.well-known/oauth-protected-resource", metadata, methods=["GET"]),
        Route("/healthz", health, methods=["GET"]),
        Route("/metrics", prometheus, methods=["GET"]),
        *web.routes(),
    ]
    app = Starlette(
        routes=routes,
        middleware=[Middleware(GatewayAuthMiddleware, verifier=verifier, metrics=metrics)],
        lifespan=lifespan,
    )
    return app, gateway
