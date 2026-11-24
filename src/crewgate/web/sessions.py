"""Server-side sessions. The cookie holds a random id; everything else stays here.

In memory, one process: right for a single-instance demo. A multi-instance deployment would put
this in Redis with the same interface.
"""

import secrets
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

from crewgate.agent.llm import Message
from crewgate.agent.reply_guard import Facts


@dataclass
class Tokens:
    access: str
    refresh: str | None
    expires_at: float
    username: str
    subject: str


@dataclass
class Session:
    id: str
    created: float
    last_seen: float
    login: dict[str, str] | None = None  # state, nonce, PKCE verifier while signing in
    tokens: Tokens | None = None
    history: list[Message] = field(default_factory=list)
    facts: Facts = field(default_factory=lambda: Facts({}, {}))
    turns: list[float] = field(default_factory=list)  # when each chat turn started
    log: list[dict[str, Any]] = field(default_factory=list)  # what the page shows per turn


class SessionStore:
    def __init__(self, ttl: float, clock: Callable[[], float] = time.time) -> None:
        self.ttl, self.clock = ttl, clock
        self._items: dict[str, Session] = {}

    def new(self) -> Session:
        self._sweep()
        now = self.clock()
        s = Session(secrets.token_urlsafe(32), now, now)
        self._items[s.id] = s
        return s

    def get(self, sid: str | None) -> Session | None:
        if not sid:
            return None
        s = self._items.get(sid)
        if s is None or self.clock() - s.last_seen > self.ttl:
            self._items.pop(sid, None)
            return None
        s.last_seen = self.clock()
        return s

    def drop(self, sid: str | None) -> None:
        if sid:
            self._items.pop(sid, None)

    def _sweep(self) -> None:
        now = self.clock()
        for sid in [k for k, v in self._items.items() if now - v.last_seen > self.ttl]:
            del self._items[sid]

    def __len__(self) -> int:
        return len(self._items)
