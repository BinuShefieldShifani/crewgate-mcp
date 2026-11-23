"""Plugin contract. Each domain (crew, booking) ships one Plugin; the core loads them by name.

The core owns security. A plugin only declares its tools and the scope each one needs,
so a plugin can never register a tool that skips auth, entitlements, rate limits or audit.
"""

from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass
from importlib import import_module
from typing import TYPE_CHECKING, Any, Protocol

from pydantic import BaseModel

if TYPE_CHECKING:
    from .approvals import ApprovalStore
    from .audit import AuditLog
    from .auth import TokenExchanger
    from .metrics import GatewayMetrics

ToolHandler = Callable[..., Awaitable[Any]]


@dataclass(frozen=True)
class ToolSpec:
    name: str  # globally unique, prefixed with the plugin name, e.g. "crew.get_roster"
    description: str  # what the model reads to decide whether to call the tool
    required_scope: str  # e.g. "roster.read"
    handler: ToolHandler  # async (principal, **validated_args) -> output
    writes: bool = False  # write tools need human approval before they run
    market_bound: bool = False  # the call must name a market, checked against entitlements
    title: str | None = None
    input_model: type[BaseModel] | None = None  # strict argument schema, published to clients
    output_model: type[BaseModel] | None = None  # structured result schema
    idempotent: bool = False
    # For write tools: one plain sentence the human sees before approving, built from the arguments.
    summarize: Callable[[Mapping[str, Any]], str] | None = None


@dataclass(frozen=True)
class PluginContext:
    """What the core hands each plugin: shared services, never secrets of other plugins."""

    exchanger: "TokenExchanger | None" = None
    metrics: "GatewayMetrics | None" = None
    approvals: "ApprovalStore | None" = None
    audit: "AuditLog | None" = None  # for events that don't come from a tool call, e.g. webhooks
    partner_transport: Any = None  # tests only: route partner calls to an in-process app
    description_variant: str = "guided"  # which tool-description variant to serve (core/descriptions.py)


class Plugin(Protocol):
    name: str

    def tools(self) -> list[ToolSpec]: ...


def plugin_routes(plugin: Plugin) -> list[Any]:
    """Extra HTTP routes a plugin serves, such as a partner's webhook. Optional."""
    routes = getattr(plugin, "routes", None)
    return list(routes()) if callable(routes) else []


class PluginLoadError(Exception):
    pass


def load_plugins(names: list[str], ctx: PluginContext | None = None) -> list[Plugin]:
    """Import crewgate.plugins.<name> and call its build(ctx) factory."""
    plugins: list[Plugin] = []
    seen: set[str] = set()
    for name in names:
        try:
            module = import_module(f"crewgate.plugins.{name}")
        except ModuleNotFoundError as exc:
            raise PluginLoadError(f"unknown plugin: {name}") from exc
        plugin: Plugin = module.build(ctx or PluginContext())
        for spec in plugin.tools():
            if not spec.name.startswith(f"{plugin.name}."):
                raise PluginLoadError(f"tool {spec.name} must be prefixed with '{plugin.name}.'")
            if spec.name in seen:
                raise PluginLoadError(f"duplicate tool name: {spec.name}")
            if spec.writes and spec.summarize is None:
                raise PluginLoadError(f"write tool {spec.name} needs a summarize() for the approver")
            seen.add(spec.name)
        plugins.append(plugin)
    return plugins
