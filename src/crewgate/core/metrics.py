"""Prometheus metrics for the gateway.

Labels are kept low-cardinality on purpose: tool, client app, outcome and reason. Never a
user id, crew id or anything personal, both for GDPR and because high-cardinality labels can
take a Prometheus server down.
"""

from prometheus_client import CollectorRegistry, Counter, Gauge, Histogram, generate_latest

LATENCY_BUCKETS = (0.005, 0.01, 0.025, 0.05, 0.1, 0.25, 0.5, 1.0, 2.5, 5.0, 10.0)


class GatewayMetrics:
    """One registry per gateway, so tests and multiple apps in one process don't collide."""

    def __init__(self, registry: CollectorRegistry | None = None) -> None:
        self.registry = registry or CollectorRegistry()
        r = self.registry
        self.tool_calls = Counter(
            "crewgate_tool_calls_total", "Tool calls by outcome", ["tool", "client", "outcome"], registry=r
        )
        self.tool_latency = Histogram(
            "crewgate_tool_duration_seconds",
            "Tool call duration",
            ["tool"],
            buckets=LATENCY_BUCKETS,
            registry=r,
        )
        self.denials = Counter(
            "crewgate_denials_total", "Calls refused before running", ["tool", "client", "reason"], registry=r
        )
        self.auth_failures = Counter(
            "crewgate_auth_failures_total", "Rejected tokens by reason", ["reason"], registry=r
        )
        self.rate_limited = Counter(
            "crewgate_rate_limited_total", "Calls refused by a limit", ["client", "limit"], registry=r
        )
        self.partner_requests = Counter(
            "crewgate_partner_requests_total",
            "Calls to partner APIs",
            ["partner", "status_class"],
            registry=r,
        )
        self.partner_retries = Counter(
            "crewgate_partner_retries_total", "Partner retries by cause", ["partner", "cause"], registry=r
        )
        self.token_exchanges = Counter(
            "crewgate_token_exchanges_total", "OBO token exchanges", ["audience", "outcome"], registry=r
        )
        self.signature_failures = Counter(
            "crewgate_signature_failures_total", "Rejected signatures", ["source", "reason"], registry=r
        )
        self.approvals = Counter(
            "crewgate_approvals_total", "Write approvals by outcome", ["tool", "outcome"], registry=r
        )
        self.audit_records = Counter("crewgate_audit_records_total", "Audit records written", registry=r)
        self.in_flight = Gauge("crewgate_tool_calls_in_flight", "Tool calls running now", registry=r)

    def exposition(self) -> bytes:
        """Text format for a /metrics endpoint."""
        return generate_latest(self.registry)

    def value(self, name: str, **labels: str) -> float:
        """Current value of a sample, for tests and the CLI summary."""
        got = self.registry.get_sample_value(name, labels or None)
        return float(got) if got is not None else 0.0
