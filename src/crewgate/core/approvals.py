"""Human approval for write tools.

A write tool never runs on the agent's say-so. The agent's call only creates a PendingAction.
The person then approves or rejects it outside MCP: they sign in again in their own browser,
through a separate OAuth client (crewgate-approvals), and that sign-in must be fresh.

Why not MCP elicitation? A form elicitation goes to the MCP client, and if that client is an
agent it may answer the question itself. The approval must come from somewhere the agent has
no credentials for.
"""

import secrets
import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any, Literal

from pydantic import BaseModel, Field

from .audit import args_digest


class ApprovalStatus(StrEnum):
    PENDING = "pending"
    EXECUTED = "executed"
    FAILED = "failed"
    REJECTED = "rejected"


class ApprovalFailure(StrEnum):
    NOT_FOUND = "approval_not_found"
    EXPIRED = "approval_expired"
    ALREADY_DECIDED = "already_decided"
    WRONG_USER = "wrong_user"
    NOT_A_HUMAN_SIGN_IN = "not_a_human_sign_in"  # token came from the agent's app, not the approval app
    STALE_SIGN_IN = "stale_sign_in"  # the person did not sign in recently enough
    MISSING_SCOPE = "missing_scope"


_STATUS = {
    ApprovalFailure.NOT_FOUND: 404,
    ApprovalFailure.EXPIRED: 410,
    ApprovalFailure.ALREADY_DECIDED: 409,
}


class ApprovalError(Exception):
    def __init__(self, reason: ApprovalFailure) -> None:
        super().__init__(reason.value)
        self.reason = reason

    @property
    def status(self) -> int:
        return _STATUS.get(self.reason, 403)


@dataclass
class PendingAction:
    id: str
    tool: str
    subject: str  # the user the agent was acting for; only they may decide
    client_id: str  # the app that proposed it
    args: dict[str, Any]
    summary: str
    created_at: float
    expires_at: float
    status: ApprovalStatus = ApprovalStatus.PENDING
    result: Any = None
    decided_by_client: str | None = None
    args_sha256: str = field(init=False)

    def __post_init__(self) -> None:
        self.args_sha256 = args_digest(self.args)

    def public(self) -> dict[str, Any]:
        """What the agent and the approval page may see. No raw result internals."""
        return {
            "approval_id": self.id,
            "tool": self.tool,
            "summary": self.summary,
            "status": self.status.value,
            "expires_at": int(self.expires_at),
        }


class ApprovalStore:
    """In memory, one gateway replica. Several replicas would need a shared store."""

    def __init__(self, ttl_seconds: float = 600.0, clock: Callable[[], float] = time.time) -> None:
        self.ttl, self.clock = ttl_seconds, clock
        self._items: dict[str, PendingAction] = {}

    def create(
        self, *, tool: str, subject: str, client_id: str, args: Mapping[str, Any], summary: str
    ) -> PendingAction:
        self._purge()
        now = self.clock()
        action = PendingAction(
            id=secrets.token_urlsafe(18),
            tool=tool,
            subject=subject,
            client_id=client_id,
            args=dict(args),
            summary=summary,
            created_at=now,
            expires_at=now + self.ttl,
        )
        self._items[action.id] = action
        return action

    def get(self, approval_id: str) -> PendingAction:
        action = self._items.get(approval_id)
        if action is None:
            raise ApprovalError(ApprovalFailure.NOT_FOUND)
        return action

    def open(self, approval_id: str) -> PendingAction:
        """A pending action that can still be decided."""
        action = self.get(approval_id)
        if action.status is not ApprovalStatus.PENDING:
            raise ApprovalError(ApprovalFailure.ALREADY_DECIDED)
        if self.clock() >= action.expires_at:
            raise ApprovalError(ApprovalFailure.EXPIRED)
        return action

    def _purge(self) -> None:
        # keep decided actions for a while so a late "approve" gets a clear 409, not a 404
        cutoff = self.clock() - self.ttl
        for key in [k for k, a in self._items.items() if a.expires_at < cutoff]:
            del self._items[key]


class ApprovalRequired(BaseModel):
    """What every write tool returns to the agent: nothing has happened yet."""

    status: Literal["awaiting_approval"] = "awaiting_approval"
    approval_id: str
    summary: str = Field(description="Exactly what will happen if the planner approves")
    approve_url: str = Field(description="The planner opens this and signs in to approve or reject")
    expires_at: int = Field(description="Unix time after which the request lapses")
    message: str = "Not done yet. A planner must approve this in their browser before it happens."


class ApprovalStatusView(BaseModel):
    approval_id: str
    tool: str
    summary: str
    status: Literal["pending", "executed", "failed", "rejected", "expired"]
    expires_at: int
    result: dict[str, Any] | None = Field(None, description="The write's result, only once executed")
