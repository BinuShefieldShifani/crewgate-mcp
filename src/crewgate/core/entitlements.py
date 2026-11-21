"""Per-caller entitlements: which tools a client app may use, in which markets, and how much.

Scopes come from the identity provider and say what the user consented to. Entitlements are
our own contract with each calling app (one per OAuth client id): an agent built for crew
planners gets crew tools, a booking assistant for Sweden gets booking tools in SE only.

Deny by default: an unknown client, an unlisted tool or an unlisted market is refused.
"""

import tomllib
from collections.abc import Mapping
from enum import StrEnum
from fnmatch import fnmatchcase
from pathlib import Path
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, field_validator


class EntitlementFailure(StrEnum):
    UNKNOWN_CLIENT = "unknown_client"
    TOOL_NOT_ENTITLED = "tool_not_entitled"
    MARKET_NOT_ENTITLED = "market_not_entitled"
    MARKET_REQUIRED = "market_required"


class EntitlementError(Exception):
    def __init__(self, reason: EntitlementFailure, detail: str) -> None:
        super().__init__(f"{reason}: {detail}")
        self.reason, self.detail = reason, detail


class Entitlement(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    client_id: str
    tools: tuple[str, ...]  # exact names or patterns such as "crew.*"
    markets: tuple[str, ...] = ("*",)  # ISO country codes, or "*" for any
    per_user_per_minute: int = Field(default=30, ge=1)
    per_client_per_minute: int = Field(default=300, ge=1)
    daily_quota: int = Field(default=10_000, ge=1)

    @field_validator("markets")
    @classmethod
    def _upper(cls, v: tuple[str, ...]) -> tuple[str, ...]:
        return tuple(m.upper() for m in v)

    def allows_tool(self, tool: str) -> bool:
        return any(fnmatchcase(tool, pattern) for pattern in self.tools)

    def allows_market(self, market: str) -> bool:
        return "*" in self.markets or market.upper() in self.markets


class EntitlementPolicy:
    def __init__(self, entitlements: Mapping[str, Entitlement]) -> None:
        self._by_client = dict(entitlements)

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> "EntitlementPolicy":
        clients = data.get("clients", {})
        return cls({cid: Entitlement(client_id=cid, **cfg) for cid, cfg in clients.items()})

    @classmethod
    def from_toml(cls, path: str | Path) -> "EntitlementPolicy":
        with open(path, "rb") as f:
            return cls.from_dict(tomllib.load(f))

    def get(self, client_id: str) -> Entitlement:
        ent = self._by_client.get(client_id)
        if ent is None:
            raise EntitlementError(EntitlementFailure.UNKNOWN_CLIENT, client_id)
        return ent

    def check(
        self, client_id: str, tool: str, market: str | None = None, *, market_bound: bool = False
    ) -> Entitlement:
        """Raise unless this client may call this tool (in this market, for market-bound tools)."""
        ent = self.get(client_id)
        if not ent.allows_tool(tool):
            raise EntitlementError(EntitlementFailure.TOOL_NOT_ENTITLED, tool)
        if market_bound and not market:
            raise EntitlementError(EntitlementFailure.MARKET_REQUIRED, tool)
        if market and not ent.allows_market(market):
            raise EntitlementError(EntitlementFailure.MARKET_NOT_ENTITLED, market.upper())
        return ent
