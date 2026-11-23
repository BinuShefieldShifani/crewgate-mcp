"""Gateway plugin: tools about the gateway itself. Loaded first, always.

gateway.approval_status lets the agent check a write it proposed, so it can tell the user the
truth ("still waiting", "done", "rejected") instead of guessing.
"""

from typing import Any

from pydantic import BaseModel, ConfigDict, Field

from crewgate.core.approvals import ApprovalError, ApprovalFailure, ApprovalStatusView, ApprovalStore
from crewgate.core.auth import Principal
from crewgate.core.descriptions import DEFAULT_VARIANT, check_table, describe
from crewgate.core.plugin import PluginContext, ToolSpec

DESCRIPTIONS: dict[str, dict[str, str]] = {
    "minimal": {"gateway.approval_status": "Get approval status."},
    "guided": {
        "gateway.approval_status": (
            "Check whether a write you proposed has been approved, rejected or done. Use it before "
            "telling the user anything about the outcome of a proposed change."
        )
    },
    "contract": {
        "gateway.approval_status": (
            "Where a proposed write stands: pending, approved, rejected, expired or executed.\n"
            "Use when: you proposed a booking or a swap and the user asks whether it went through, "
            "or before saying anything about its outcome.\n"
            "Needs: approval_id returned by booking_book_flight or crew_propose_swap.\n"
            "Returns: status, and when executed the result (for a flight, its booking_id and state).\n"
            "Next: for a flight, booking_get_booking with that booking_id before saying confirmed."
        )
    },
}


class ApprovalStatusInput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    approval_id: str = Field(
        min_length=10, max_length=64, description="The approval_id a write tool returned"
    )


class GatewayPlugin:
    name = "gateway"

    def __init__(self, approvals: ApprovalStore | None, variant: str = DEFAULT_VARIANT) -> None:
        self.approvals, self.variant = approvals, variant

    async def approval_status(self, p: Principal, approval_id: str) -> ApprovalStatusView:
        if self.approvals is None:
            raise RuntimeError("approval store not configured")
        action = self.approvals.get(approval_id)  # raises approval_not_found
        if action.subject != p.subject:  # someone else's request: behave as if it doesn't exist
            raise ApprovalError(ApprovalFailure.NOT_FOUND)
        status = action.status.value
        if status == "pending" and self.approvals.clock() >= action.expires_at:
            status = "expired"
        result: dict[str, Any] | None = None
        if status == "executed" and isinstance(action.result, BaseModel):
            result = action.result.model_dump(mode="json")
        return ApprovalStatusView(
            approval_id=action.id,
            tool=action.tool,
            summary=action.summary,
            status=status,
            expires_at=int(action.expires_at),
            result=result,
        )

    def tools(self) -> list[ToolSpec]:
        check_table(DESCRIPTIONS, ["gateway.approval_status"])
        return [
            ToolSpec(
                "gateway.approval_status",
                describe(DESCRIPTIONS, "gateway.approval_status", self.variant),
                "openid",
                self.approval_status,
                title="Approval status",
                input_model=ApprovalStatusInput,
                output_model=ApprovalStatusView,
                idempotent=True,
            )
        ]


def build(ctx: PluginContext | None = None) -> GatewayPlugin:
    ctx = ctx or PluginContext()
    return GatewayPlugin(ctx.approvals, ctx.description_variant)
