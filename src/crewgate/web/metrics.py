"""Prometheus metrics for the agent, as the console runs it."""

from prometheus_client import CollectorRegistry, Counter, Gauge, Histogram, generate_latest

from crewgate.agent.graph import TurnResult


class AgentMetrics:
    def __init__(self, registry: CollectorRegistry | None = None) -> None:
        self.registry = r = registry or CollectorRegistry()
        self.turns = Counter("crewgate_agent_turns_total", "Chat turns by outcome", ["outcome"], registry=r)
        self.guard = Counter(
            "crewgate_agent_guard_total", "Guard decisions on final replies", ["decision"], registry=r
        )
        self.blocked_writes = Counter(
            "crewgate_agent_blocked_writes_total", "Write tools the user didn't ask for", ["tool"], registry=r
        )
        self.rewrites = Counter(
            "crewgate_agent_rewrites_total", "Replies rewritten into the user's language", registry=r
        )
        self.tool_calls = Counter(
            "crewgate_agent_tool_calls_total", "Tool calls the agent made", ["tool", "outcome"], registry=r
        )
        self.model_calls = Counter("crewgate_agent_model_calls_total", "Model calls", registry=r)
        self.tokens = Counter("crewgate_agent_model_tokens_total", "Model tokens", ["kind"], registry=r)
        self.model_latency = Histogram(
            "crewgate_agent_model_seconds",
            "Model call latency",
            buckets=(0.1, 0.25, 0.5, 1, 2, 4, 8, 16, 32),
            registry=r,
        )
        self.turn_latency = Histogram(
            "crewgate_agent_turn_seconds",
            "Whole chat turn",
            buckets=(0.5, 1, 2, 4, 8, 16, 32, 64),
            registry=r,
        )
        self.sessions = Gauge("crewgate_web_sessions", "Live console sessions", registry=r)

    def record(self, turn: TurnResult, seconds: float) -> None:
        self.turns.labels("ok").inc()
        self.guard.labels("passed" if turn.guard.ok else "replaced").inc()
        self.turn_latency.observe(seconds)
        for t in turn.trace:
            if t["kind"] == "llm":
                self.model_calls.inc()
                self.model_latency.observe(t["ms"] / 1000)
                self.tokens.labels("prompt").inc(t.get("prompt_tokens", 0))
                self.tokens.labels("completion").inc(t.get("completion_tokens", 0))
            elif t["kind"] == "tool":
                self.tool_calls.labels(t["name"], "error" if t.get("error") else "ok").inc()
            elif t["kind"] == "blocked":
                self.blocked_writes.labels(t["name"]).inc()
            elif t["kind"] == "rewrite" and t.get("ok"):
                self.rewrites.inc()

    def exposition(self) -> bytes:
        return generate_latest(self.registry)
