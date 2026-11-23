"""MCP tools/list and tools/call, on the low-level SDK server so crewgate owns every step.

How outcomes map to MCP:
- success                 -> result with structuredContent matching the tool's outputSchema
- write tool              -> result {"status": "awaiting_approval", ...}: nothing has happened yet
- bad arguments, refusals, partner failures -> result with isError=true and a short, safe
  message, so the model can correct itself (MCP: tool execution errors)
- unknown tool            -> JSON-RPC error -32602 (MCP: protocol error)
- anything unexpected     -> isError result "internal_error". Exception text never leaves the
  gateway: the SDK would otherwise send it to the client as-is.
"""

import json
import logging
import re
from collections.abc import Mapping
from typing import Any

import mcp_types as types
from mcp.server.context import ServerRequestContext
from mcp.shared.exceptions import MCPError
from pydantic import BaseModel, ValidationError

from crewgate.core.approvals import ApprovalError, ApprovalRequired
from crewgate.core.auth import AuthError, Principal
from crewgate.core.errors import ErrorCategory, PartnerError
from crewgate.core.guard import GuardDenied, ToolGuard
from crewgate.core.plugin import ToolSpec

from .auth import principal_of

log = logging.getLogger("crewgate.server.tools")

_PARTNER_MESSAGES = {
    ErrorCategory.AUTH: "the partner did not accept the gateway's credentials",
    ErrorCategory.FORBIDDEN: "the partner refused: this user may not do that",
    ErrorCategory.NOT_FOUND: "not found at the partner",
    ErrorCategory.RATE_LIMITED: "the partner is rate limiting, try again later",
    ErrorCategory.VALIDATION: "the partner rejected the request as invalid",
    ErrorCategory.CONFLICT: "the partner reports a conflict (the data changed)",
    ErrorCategory.UPSTREAM_UNAVAILABLE: "the partner is unavailable, try again later",
    ErrorCategory.UNKNOWN: "the partner returned an unexpected error",
}
_SAFE_CODE = re.compile(r"^[a-z_]{3,40}$")


def mcp_name(spec: ToolSpec) -> str:
    """crew.get_roster -> crew_get_roster. Many clients only accept [A-Za-z0-9_-]."""
    return spec.name.replace(".", "_")


def _schema(model: type[BaseModel] | None, mode: str) -> dict[str, Any]:
    if model is None:
        return {"type": "object"}
    return model.model_json_schema(mode=mode)  # type: ignore[arg-type]


def tool_definition(spec: ToolSpec) -> types.Tool:
    output = ApprovalRequired if spec.writes else spec.output_model
    return types.Tool(
        name=mcp_name(spec),
        title=spec.title,
        description=spec.description,
        inputSchema=_schema(spec.input_model, "validation"),
        outputSchema=_schema(output, "serialization"),
        annotations=types.ToolAnnotations(
            title=spec.title,
            readOnlyHint=not spec.writes,
            destructiveHint=spec.writes,
            idempotentHint=spec.idempotent,
            openWorldHint=True,
        ),
    )


def _error(code: str, message: str, **extra: Any) -> types.CallToolResult:
    body = {"error": code, "message": message, **extra}
    return types.CallToolResult(isError=True, content=[types.TextContent(type="text", text=json.dumps(body))])


def _ok(data: dict[str, Any]) -> types.CallToolResult:
    return types.CallToolResult(
        content=[types.TextContent(type="text", text=json.dumps(data, default=str))], structuredContent=data
    )


class ToolRouter:
    def __init__(self, specs: list[ToolSpec], guard: ToolGuard, approve_url: str) -> None:
        self.by_name = {mcp_name(s): s for s in specs}
        self.guard, self.approve_url = guard, approve_url.rstrip("/")

    async def list_tools(
        self, ctx: ServerRequestContext[Any], params: types.PaginatedRequestParams | None
    ) -> types.ListToolsResult:
        return types.ListToolsResult(tools=[tool_definition(s) for s in self.by_name.values()])

    async def call_tool(
        self, ctx: ServerRequestContext[Any], params: types.CallToolRequestParams
    ) -> types.CallToolResult:
        principal = principal_of(ctx.request.scope) if ctx.request is not None else None
        if principal is None:  # RequirePrincipal stops this earlier; belt and braces
            raise MCPError(types.INVALID_REQUEST, "not authenticated")
        spec = self.by_name.get(params.name)
        if spec is None:
            raise MCPError(types.INVALID_PARAMS, f"Unknown tool: {params.name}")
        return await self.call(principal, spec, params.arguments or {})

    async def call(
        self, principal: Principal, spec: ToolSpec, arguments: Mapping[str, Any]
    ) -> types.CallToolResult:
        try:
            args = self._validate(spec, arguments)
        except ValidationError as exc:
            details = [
                {"field": ".".join(str(p) for p in e["loc"]), "problem": e["msg"]} for e in exc.errors()
            ]
            return _error(
                "invalid_arguments", "the arguments do not match the tool's input schema", details=details
            )

        market = args.get("market") if spec.market_bound else None
        try:
            if spec.writes:
                action = await self.guard.propose(principal, spec, args, market)
                pending = ApprovalRequired(
                    approval_id=action.id,
                    summary=action.summary,
                    approve_url=f"{self.approve_url}/approvals/{action.id}",
                    expires_at=int(action.expires_at),
                )
                return _ok(pending.model_dump(mode="json"))
            result = await self.guard.run(principal, spec, args, market)
            return _ok(self._dump(spec, result))
        except GuardDenied as exc:
            extra = {"retry_after": int(exc.headers["Retry-After"])} if "Retry-After" in exc.headers else {}
            return _error(exc.reason, exc.message, **extra)
        except PartnerError as exc:
            if exc.code and exc.code.endswith("_not_configured"):
                return _error(
                    exc.code, "this feature is not set up on this gateway (missing partner credentials)"
                )
            detail = exc.message if _SAFE_CODE.match(exc.message) else None
            return _error(
                f"partner_{exc.category.value}",
                _PARTNER_MESSAGES[exc.category],
                **({"detail": detail} if detail else {}),
            )
        except AuthError as exc:
            return _error(exc.code.value, exc.description)
        except ApprovalError as exc:
            return _error(exc.reason.value, "no such approval request for this user")
        except Exception:
            log.exception("tool_crashed", extra={"tool": spec.name})
            return _error("internal_error", "the gateway hit an unexpected error")

    @staticmethod
    def _validate(spec: ToolSpec, arguments: Mapping[str, Any]) -> dict[str, Any]:
        if spec.input_model is None:
            return dict(arguments)
        return dict(spec.input_model.model_validate(arguments))

    @staticmethod
    def _dump(spec: ToolSpec, result: Any) -> dict[str, Any]:
        if spec.output_model is not None and not isinstance(result, BaseModel):
            result = spec.output_model.model_validate(result)
        if isinstance(result, BaseModel):
            return result.model_dump(mode="json")
        if isinstance(result, dict):
            return result
        return {"result": result}
