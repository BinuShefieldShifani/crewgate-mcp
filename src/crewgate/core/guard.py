"""The policy enforcement point: every tool call goes through ToolGuard, nothing else.

Read tools:   run()      entitlement -> scope -> rate limits -> handler
Write tools:  propose()  entitlement -> scope -> rate limits -> parked as a PendingAction
              decide()   same person, fresh sign-in through the approval app -> handler

Every outcome, allowed or refused, success or failure, is written to the audit chain and to
metrics. A refusal never reaches the plugin and never spends rate-limit budget. run() refuses
write tools outright, so there is no path from the agent to a write without a human.
"""

import time
from collections.abc import Callable, Collection, Mapping
from typing import Any

from .approvals import ApprovalError, ApprovalFailure, ApprovalStatus, ApprovalStore, PendingAction
from .audit import AuditLog, Decision
from .auth import AuthError, Principal
from .entitlements import EntitlementError, EntitlementPolicy
from .errors import PartnerError
from .metrics import GatewayMetrics
from .plugin import ToolSpec
from .ratelimit import RateLimiter


class GuardDenied(Exception):
    """A refusal the transport layer turns into a response (403 or 429 with headers)."""

    def __init__(
        self, status: int, reason: str, message: str, headers: Mapping[str, str] | None = None
    ) -> None:
        super().__init__(f"{reason}: {message}")
        self.status, self.reason, self.message = status, reason, message
        self.headers = dict(headers or {})


class ToolGuard:
    def __init__(
        self,
        policy: EntitlementPolicy,
        limiter: RateLimiter,
        audit: AuditLog,
        metrics: GatewayMetrics,
        approvals: ApprovalStore | None = None,
        *,
        approval_clients: Collection[str] = ("crewgate-approvals",),
        max_sign_in_age_seconds: float = 300.0,
        timer: Callable[[], float] = time.perf_counter,
        clock: Callable[[], float] = time.time,
    ) -> None:
        self.policy, self.limiter, self.audit, self.metrics, self.timer = (
            policy,
            limiter,
            audit,
            metrics,
            timer,
        )
        self.approvals = approvals or ApprovalStore(clock=clock)
        self.approval_clients = frozenset(approval_clients)
        self.max_sign_in_age = max_sign_in_age_seconds
        self.clock = clock

    # ---- reads --------------------------------------------------------------------------

    async def run(
        self, principal: Principal, spec: ToolSpec, args: Mapping[str, Any], market: str | None = None
    ) -> Any:
        started = self.timer()
        if spec.writes:
            raise self._deny(
                principal, spec, args, started, 409, "approval_required", "write tools need approval"
            )
        self._check(principal, spec, args, market, started)
        return await self._execute(principal, spec, args, started, reason="ok")

    # ---- writes -------------------------------------------------------------------------

    async def propose(
        self, principal: Principal, spec: ToolSpec, args: Mapping[str, Any], market: str | None = None
    ) -> PendingAction:
        started = self.timer()
        if not spec.writes or spec.summarize is None:
            raise ValueError(f"{spec.name} is not a write tool")
        self._check(principal, spec, args, market, started)
        action = self.approvals.create(
            tool=spec.name,
            subject=principal.subject,
            client_id=principal.client_id,
            args=args,
            summary=spec.summarize(args),
        )
        self.metrics.approvals.labels(spec.name, "requested").inc()
        self._audit(principal, spec, args, Decision.PENDING_APPROVAL, f"approval:{action.id}", "-", started)
        return action

    async def decide(
        self, approval_id: str, approver: Principal, spec: ToolSpec, *, approve: bool
    ) -> PendingAction:
        """Run or drop a pending write. `approver` must be the same person, freshly signed in."""
        started = self.timer()
        action = self.approvals.open(approval_id)  # raises not found / expired / already decided
        try:
            self._check_approver(action, approver, spec)
        except ApprovalError as exc:
            self.metrics.approvals.labels(spec.name, exc.reason.value).inc()
            self._audit(approver, spec, action.args, Decision.DENIED, exc.reason.value, "-", started)
            raise

        action.decided_by_client = approver.client_id
        if not approve:
            action.status = ApprovalStatus.REJECTED
            self.metrics.approvals.labels(spec.name, "rejected").inc()
            self._audit(approver, spec, action.args, Decision.DENIED, "rejected_by_user", "-", started)
            return action

        self.metrics.approvals.labels(spec.name, "approved").inc()
        try:
            action.result = await self._execute(
                approver, spec, action.args, started, reason=f"approved:{action.id}"
            )
        except Exception:
            action.status = ApprovalStatus.FAILED
            raise
        action.status = ApprovalStatus.EXECUTED
        return action

    def _check_approver(self, action: PendingAction, approver: Principal, spec: ToolSpec) -> None:
        if approver.client_id not in self.approval_clients:
            raise ApprovalError(ApprovalFailure.NOT_A_HUMAN_SIGN_IN)
        if approver.subject != action.subject:
            raise ApprovalError(ApprovalFailure.WRONG_USER)
        if approver.auth_time is None or self.clock() - approver.auth_time > self.max_sign_in_age:
            raise ApprovalError(ApprovalFailure.STALE_SIGN_IN)
        if spec.required_scope not in approver.scopes:
            raise ApprovalError(ApprovalFailure.MISSING_SCOPE)

    # ---- shared ---------------------------------------------------------------------------

    def _check(
        self,
        principal: Principal,
        spec: ToolSpec,
        args: Mapping[str, Any],
        market: str | None,
        started: float,
    ) -> None:
        client = principal.client_id
        try:
            ent = self.policy.check(client, spec.name, market, market_bound=spec.market_bound)
        except EntitlementError as exc:
            raise self._deny(
                principal, spec, args, started, 403, exc.reason.value, f"client is not entitled: {exc.detail}"
            ) from exc
        try:
            principal.require_scopes([spec.required_scope])
        except AuthError as exc:
            raise self._deny(
                principal,
                spec,
                args,
                started,
                403,
                "insufficient_scope",
                f"needs scope {spec.required_scope}",
            ) from exc
        decision = self.limiter.check(
            client,
            principal.subject,
            per_user_per_minute=ent.per_user_per_minute,
            per_client_per_minute=ent.per_client_per_minute,
            daily_quota=ent.daily_quota,
        )
        if not decision.allowed:
            self.metrics.rate_limited.labels(client, decision.limit_name).inc()
            raise self._deny(
                principal, spec, args, started, 429, f"rate_limited:{decision.limit_name}", "slow down",
                decision.headers(),
            )  # fmt: skip

    def _deny(
        self,
        principal: Principal,
        spec: ToolSpec,
        args: Mapping[str, Any],
        started: float,
        status: int,
        reason: str,
        message: str,
        headers: Mapping[str, str] | None = None,
    ) -> GuardDenied:
        self.metrics.denials.labels(spec.name, principal.client_id, reason).inc()
        self._audit(principal, spec, args, Decision.DENIED, reason, "-", started)
        return GuardDenied(status, reason, message, headers)

    async def _execute(
        self, principal: Principal, spec: ToolSpec, args: Mapping[str, Any], started: float, *, reason: str
    ) -> Any:
        self.metrics.in_flight.inc()
        outcome = "ok"
        try:
            return await spec.handler(principal, **args)
        except PartnerError as exc:
            outcome = f"error:{exc.category.value}"
            raise
        except AuthError as exc:
            outcome = f"error:{exc.code.value}"
            raise
        except ApprovalError as exc:
            outcome = f"error:{exc.reason.value}"
            raise
        except Exception:
            outcome = "error:internal"
            raise
        finally:
            self.metrics.in_flight.dec()
            self.metrics.tool_calls.labels(spec.name, principal.client_id, outcome).inc()
            self.metrics.tool_latency.labels(spec.name).observe(self.timer() - started)
            self._audit(principal, spec, args, Decision.ALLOWED, reason, outcome, started)

    def _audit(
        self,
        principal: Principal,
        spec: ToolSpec,
        args: Mapping[str, Any],
        decision: Decision,
        reason: str,
        outcome: str,
        started: float,
    ) -> None:
        self.audit.record(
            subject=principal.subject,
            client_id=principal.client_id,
            tool=spec.name,
            decision=decision,
            reason=reason,
            outcome=outcome,
            args=dict(args),
            duration_ms=(self.timer() - started) * 1000,
        )
        self.metrics.audit_records.inc()
