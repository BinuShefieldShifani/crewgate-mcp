"""Plugin contract. Each domain (crew, booking) ships one Plugin; the core loads them by name.

The core owns security. A plugin only declares its tools and the scope each one needs,
so a plugin can never register a tool that skips auth, entitlements, rate limits or audit.
"""

from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from importlib import import_module
from typing import Any, Protocol

ToolHandler = Callable[..., Awaitable[Any]]


@dataclass(frozen=True)
class ToolSpec:
    name: str  # globally unique, prefixed with the plugin name, e.g. "crew.get_roster"
    description: str
    required_scope: str  # e.g. "roster.read"
    handler: ToolHandler
    writes: bool = False  # write tools need human approval before they run


class Plugin(Protocol):
    name: str

    def tools(self) -> list[ToolSpec]: ...


class PluginLoadError(Exception):
    pass


def load_plugins(names: list[str]) -> list[Plugin]:
    """Import crewgate.plugins.<name> and call its build() factory."""
    plugins: list[Plugin] = []
    seen: set[str] = set()
    for name in names:
        try:
            module = import_module(f"crewgate.plugins.{name}")
        except ModuleNotFoundError as exc:
            raise PluginLoadError(f"unknown plugin: {name}") from exc
        plugin: Plugin = module.build()
        for spec in plugin.tools():
            if not spec.name.startswith(f"{plugin.name}."):
                raise PluginLoadError(f"tool {spec.name} must be prefixed with '{plugin.name}.'")
            if spec.name in seen:
                raise PluginLoadError(f"duplicate tool name: {spec.name}")
            seen.add(spec.name)
        plugins.append(plugin)
    return plugins
