"""The policy enforcement point: every tool call goes through ToolGuard.run, nothing else.

Order of checks, cheapest and most fundamental first:
  1. entitlement   - may this client app use this tool, in this market?
  2. scope         - did the user grant the scope this tool needs?
  3. rate limits   - per user, per app, daily quota
  4. run           - the plugin's handler, with the verified Principal

Every outcome, allowed or refused, success or failure, is written to the audit chain and to
metrics. A refusal never reaches the plugin, and never spends rate-limit budget.
"""

import time
from collections.abc import Callable, Mapping
from typing import Any

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
        timer: Callable[[], float] = time.perf_counter,
    ) -> None:
        self.policy, self.limiter, self.audit, self.metrics, self.timer = (
            policy,
            limiter,
            audit,
            metrics,
            timer,
        )

    async def run(
        self, principal: Principal, spec: ToolSpec, args: Mapping[str, Any], market: str | None = None
    ) -> Any:
        started = self.timer()
        client = principal.client_id

        def deny(
            status: int, reason: str, message: str, headers: Mapping[str, str] | None = None
        ) -> GuardDenied:
            self.metrics.denials.labels(spec.name, client, reason).inc()
            self._audit(principal, spec, args, Decision.DENIED, reason, "-", started)
            return GuardDenied(status, reason, message, headers)

        try:
            ent = self.policy.check(client, spec.name, market, market_bound=spec.market_bound)
        except EntitlementError as exc:
            raise deny(403, exc.reason.value, f"client is not entitled: {exc.detail}") from exc

        try:
            principal.require_scopes([spec.required_scope])
        except AuthError as exc:
            raise deny(403, "insufficient_scope", f"needs scope {spec.required_scope}") from exc

        decision = self.limiter.check(
            client,
            principal.subject,
            per_user_per_minute=ent.per_user_per_minute,
            per_client_per_minute=ent.per_client_per_minute,
            daily_quota=ent.daily_quota,
        )
        if not decision.allowed:
            self.metrics.rate_limited.labels(client, decision.limit_name).inc()
            raise deny(429, f"rate_limited:{decision.limit_name}", "slow down", decision.headers())

        self.metrics.in_flight.inc()
        outcome = "ok"
        try:
            return await spec.handler(principal, **args)
        except PartnerError as exc:
            outcome = f"error:{exc.category.value}"
            raise
        except Exception:
            outcome = "error:internal"
            raise
        finally:
            self.metrics.in_flight.dec()
            self.metrics.tool_calls.labels(spec.name, client, outcome).inc()
            self.metrics.tool_latency.labels(spec.name).observe(self.timer() - started)
            self._audit(principal, spec, args, Decision.ALLOWED, "ok", outcome, started)

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
